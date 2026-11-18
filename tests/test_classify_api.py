# -*- coding: utf-8 -*-
"""稿件归档 API 测试：提交判定、决策规则、改稿后迟到结果不覆盖当前稿件、重启保留。"""
from __future__ import annotations

import time

import pytest

AI_TEXT = (
    "本文讨论大语言模型的微调策略：冻结主干网络、注入适配层，"
    "用小规模标注数据做梯度更新，并在独立测试集上评估泛化能力与幻觉比例，"
    "同时关注训练算力调度与显存占用。"
)
COOK_TEXT = (
    "本文介绍家常红烧肉的炖煮方法：冷水下锅焯去血沫，糖色上色后小火慢炖，"
    "出锅前半小时再加盐，关火焖一会儿让味道渗入纤维，搭配白米饭最香。"
)
SPORT_TEXT = (
    "本文分析足球俱乐部的战术数据：预期进球模型综合射门位置与防守压力，"
    "教练通过热图发现中场覆盖盲区，定位球战术经过数据打磨。"
)
MIX_TEXT = (
    "本文前半讨论大语言模型的微调策略：冻结主干网络、注入适配层，用小规模标注数据做梯度更新，"
    "评估泛化能力与幻觉比例；后半介绍家常红烧肉的炖煮方法：冷水下锅焯去血沫，糖色上色后小火慢炖，"
    "出锅前半小时再加盐，关火焖一会儿让味道渗入纤维。"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立数据库文件；模块与 worker 全程单例。"""
    from app import database, main

    main.get_worker().stop()
    db_path = tmp_path / "classify.db"
    monkeypatch.setenv("RECOMMENDER_DB", str(db_path))
    monkeypatch.setenv("REVEAL_CLAIM_TTL", "2")

    database._DB_PATH = str(db_path)
    database._CLAIM_TTL_SECONDS = 2.0
    database._reset_reveal_batch()
    database.init_db()
    main.get_worker().start()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c, database, main.get_worker()
    main.get_worker().stop()


def _wait_classify_settled(c, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        data = c.get("/api/status").json()
        job = data["classify_job"]
        if job and job["status"] in ("done", "failed", "stale"):
            return data
        time.sleep(0.05)
    raise AssertionError("归档判定未在预期时间内结束")


def test_classify_flow_suggests_archive_with_scores_and_evidence(client):
    c, _, _ = client
    r = c.put("/api/manuscript", json={"text": AI_TEXT})
    assert r.status_code == 200
    assert r.json()["manuscript"]["generation"] == 1

    data = _wait_classify_settled(c)
    res = data["classify_result"]
    assert res is not None
    # 恰好一个主题达到阈值 → 建议归档
    assert res["decision"]["action"] == "archive"
    assert res["decision"]["topic"] == "人工智能"
    # 页面所需的各主题得分与依据文章
    assert len(res["topics"]) == 6
    for t in res["topics"]:
        assert 0.0 <= t["score"] <= 1.0
        assert t["evidence"]["article_id"] >= 1
        assert t["evidence"]["title"]
    reached = [t for t in res["topics"] if t["score"] >= res["threshold"]]
    assert len(reached) == 1 and reached[0]["topic"] == "人工智能"
    # 阈值来自按作者整组留出校准，并随状态返回
    assert res["threshold"] == data["calibration"]["threshold"]
    assert data["calibration"]["folds"] == 10
    assert data["calibration"]["coverage"] >= 0.9


def test_classify_mixed_manuscript_requires_review(client):
    c, _, _ = client
    r = c.put("/api/manuscript", json={"text": MIX_TEXT})
    assert r.status_code == 200
    data = _wait_classify_settled(c)
    res = data["classify_result"]
    assert res["decision"]["action"] == "review"
    assert "多个主题" in res["decision"]["reason"]
    reached = [t["topic"] for t in res["topics"] if t["score"] >= res["threshold"]]
    assert len(reached) >= 2


def test_manuscript_validation(client):
    c, _, _ = client
    assert c.put("/api/manuscript", json={"text": ""}).status_code == 422
    assert c.put("/api/manuscript", json={"text": "   \n  "}).status_code == 422
    assert c.put("/api/manuscript", json={"text": "字" * 2001}).status_code == 422
    ok = c.put("/api/manuscript", json={"text": "字" * 2000})
    assert ok.status_code == 200


def test_late_result_never_overwrites_current_manuscript(client):
    c, database, _ = client
    # 慢判定 A（体育向，2 秒延迟）
    r1 = c.put("/api/manuscript", json={
        "text": SPORT_TEXT, "compute_delay_ms": 2000,
    }).json()
    gen_a = r1["manuscript"]["generation"]
    assert r1["classify_job"]["status"] in ("pending", "running")

    # 判定窗口内立刻改稿 B（美食向，无延迟）
    r2 = c.put("/api/manuscript", json={
        "text": COOK_TEXT, "compute_delay_ms": 0,
    }).json()
    gen_b = r2["manuscript"]["generation"]
    assert gen_b == gen_a + 1

    # 等待两个世代都结算
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        ja = database.get_classify_job(gen_a)
        jb = database.get_classify_job(gen_b)
        if jb["status"] == "done" and ja["status"] in ("stale", "done", "failed"):
            break
        time.sleep(0.05)

    ja = database.get_classify_job(gen_a)
    jb = database.get_classify_job(gen_b)
    # 旧稿件的迟到结果被拒绝落库；新稿件判定完成
    assert ja["status"] == "stale"
    assert jb["status"] == "done"

    status = c.get("/api/status").json()
    res = status["classify_result"]
    assert res is not None
    assert status["classify_job"]["generation"] == gen_b
    # 当前结果对应 B 稿件（美食），不是 A（体育）
    assert res["decision"]["action"] == "archive"
    assert res["decision"]["topic"] == "美食烹饪"
    assert res["text_preview"] == COOK_TEXT[:80]


def test_restart_retains_manuscript_and_result(tmp_path, monkeypatch):
    db_file = tmp_path / "restart_classify.db"
    monkeypatch.setenv("RECOMMENDER_DB", str(db_file))

    from app import database, main
    from fastapi.testclient import TestClient

    main.get_worker().stop()
    database._DB_PATH = str(db_file)
    database._CLAIM_TTL_SECONDS = 0.01
    database._reset_reveal_batch()
    database.init_db()
    main.get_worker().start()
    with TestClient(main.app) as c1:
        c1.put("/api/manuscript", json={"text": AI_TEXT})
        before = _wait_classify_settled(c1)
        assert before["classify_result"]["decision"]["topic"] == "人工智能"
    main.get_worker().stop()

    # 模拟进程重启：数据库文件保留，稿件与判定结果完整恢复
    database.init_db()
    main.get_worker().start()
    try:
        with TestClient(main.app) as c2:
            after = c2.get("/api/status").json()
            assert after["manuscript"]["text"] == AI_TEXT
            assert after["manuscript"]["generation"] == \
                before["manuscript"]["generation"]
            assert after["classify_result"] is not None
            assert after["classify_result"]["decision"]["topic"] == "人工智能"
    finally:
        main.get_worker().stop()


def test_reset_clears_manuscript(client):
    c, _, _ = client
    c.put("/api/manuscript", json={"text": AI_TEXT})
    data = _wait_classify_settled(c)
    assert data["classify_result"] is not None

    r = c.post("/api/reset")
    assert r.status_code == 200
    payload = r.json()
    assert payload["manuscript"]["text"] == ""
    assert payload["manuscript"]["generation"] == 0
    assert payload["classify_result"] is None
    # 推荐功能在重置后仍然可用
    assert payload["job"] is not None

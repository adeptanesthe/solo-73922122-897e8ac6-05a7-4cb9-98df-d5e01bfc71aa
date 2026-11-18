# -*- coding: utf-8 -*-
"""稿件归档：主题评分、决策规则与按作者整组留出校准的单元测试。"""
import math

from app.articles_data import ARTICLES, TOPICS
from app.classifier import (
    REQUIRED_COVERAGE,
    decide,
    get_calibration,
    score_topics,
)
from app.tfidf_model import get_model


def test_score_topics_covers_all_topics_with_valid_evidence():
    scores = score_topics("神经网络训练与深度学习模型优化")
    assert [s["topic"] for s in scores] == TOPICS  # 六个主题齐全、顺序固定
    for s in scores:
        assert 0.0 <= s["score"] <= 1.0
        ev = s["evidence"]
        art = next(a for a in ARTICLES if a["id"] == ev["article_id"])
        # 依据文章必须属于该主题，且其相似度就是主题得分（最高余弦相似度）
        assert art["topic"] == s["topic"]
        assert ev["similarity"] == s["score"]
        assert ev["title"] == art["title"]


def test_score_is_max_cosine_over_topic_articles():
    # 主题得分应等于该主题全部文章余弦相似度的最大值
    from sklearn.metrics.pairwise import cosine_similarity

    text = "马拉松训练与耐力提升，长距离慢跑与间歇跑安排"
    model = get_model()
    sims = cosine_similarity(model.transform_interest(text), model.article_matrix)[0]
    for s in score_topics(text):
        topic_sims = [
            float(sim)
            for a, sim in zip(ARTICLES, sims)
            if a["topic"] == s["topic"]
        ]
        assert s["score"] == max(0.0, min(1.0, max(topic_sims)))


def test_known_body_top_topic_is_its_own():
    target = next(a for a in ARTICLES if a["id"] == 22)  # 家常炖煮，美食烹饪
    scores = score_topics(target["body"])
    best = max(scores, key=lambda s: s["score"])
    assert best["topic"] == "美食烹饪"
    assert best["evidence"]["article_id"] == 22
    assert best["score"] > 0.9


def test_decide_exactly_one_reached_archives():
    topics = [{"topic": t, "score": 0.1} for t in TOPICS]
    topics[2]["score"] = 0.5
    d = decide(topics, 0.3)
    assert d["action"] == "archive"
    assert d["topic"] == TOPICS[2]
    # 决策函数会回写 reaches_threshold 标记
    assert sum(1 for t in topics if t["reaches_threshold"]) == 1


def test_decide_none_reached_requires_review():
    topics = [{"topic": t, "score": 0.1} for t in TOPICS]
    d = decide(topics, 0.3)
    assert d["action"] == "review"
    assert d["topic"] is None
    assert "没有" in d["reason"]


def test_decide_multiple_reached_requires_review():
    topics = [{"topic": t, "score": 0.1} for t in TOPICS]
    topics[0]["score"] = 0.9
    topics[1]["score"] = 0.8
    d = decide(topics, 0.3)
    assert d["action"] == "review"
    assert d["topic"] is None
    assert "多个" in d["reason"]
    assert TOPICS[0] in d["reason"] and TOPICS[1] in d["reason"]


def test_calibration_leave_one_author_out_refit_per_fold():
    cal = get_calibration()
    assert cal.folds == 10           # 10 位作者 → 10 折
    assert cal.held_out == 30        # 每篇留出文章贡献一个真主题得分
    assert len(cal.true_scores_desc) == 30
    authors = set()
    for f in cal.fold_details:
        assert f["n_train"] == 27    # 留出一位作者的 3 篇后剩 27 篇训练
        assert f["n_held_out"] == 3
        assert len(f["true_scores"]) == 3
        assert f["n_features"] > 100
        authors.add(f["held_out_author"])
    assert len(authors) == 10        # 每位作者恰好留出一折
    # 每折特征在 27 篇训练文上重新拟合，词表规模与全量 30 篇拟合的生产模型不同
    full_features = get_model().feature_count
    assert all(f["n_features"] != full_features for f in cal.fold_details)


def test_calibration_threshold_is_highest_with_90pct_coverage():
    cal = get_calibration()
    need = math.ceil(30 * REQUIRED_COVERAGE - 1e-9)
    assert need == 27                # 至少 90% 的 30 篇留出文章
    # 阈值 = 满足条件的最高的那个：降序第 27 个真主题得分
    assert cal.threshold == cal.true_scores_desc[need - 1]
    assert sum(1 for s in cal.true_scores_desc if s >= cal.threshold) >= need
    assert cal.coverage >= REQUIRED_COVERAGE
    # 最高性：任何比阈值大的留出得分都无法凑够 27 篇
    higher = [s for s in cal.true_scores_desc if s > cal.threshold]
    if higher:
        tau2 = min(higher)
        assert sum(1 for s in cal.true_scores_desc if s >= tau2) < need


def test_calibration_summary_json_shape():
    summary = get_calibration().summary()
    assert 0.0 <= summary["threshold"] <= 1.0
    assert summary["required_coverage"] == REQUIRED_COVERAGE
    assert summary["folds"] == 10
    assert summary["held_out"] == 30
    assert len(summary["fold_details"]) == 10

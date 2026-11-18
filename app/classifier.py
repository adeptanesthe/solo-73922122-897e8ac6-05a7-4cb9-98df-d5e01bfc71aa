# -*- coding: utf-8 -*-
"""稿件主题归档：主题评分、归档决策与按作者整组留出的阈值校准。

评分
====
生产模型在全部 30 篇已标注文章正文上拟合字符 2~3 元 TF-IDF（见 tfidf_model.py），
稿件对每个主题的得分 = 稿件与该主题全部文章余弦相似度的**最高值**，
取得最高值的那篇文章即为该主题的“依据文章”。

校准
====
按作者整组留出（Leave-One-Author-Out）：10 位作者 → 10 折。
每一折用其余 9 位作者的 27 篇文章**重新拟合** TF-IDF 特征（新建向量器实例），
再计算每篇留出文章的“真主题得分”：它与训练集中其真主题文章的最高余弦相似度。
30 篇留出文章的真主题得分汇总后，取满足
“至少 90%（30 篇中 ≥ 27 篇）留出文章真主题得分 ≥ τ”的**最高阈值** τ，
即降序排列后的第 27 个得分。

决策
====
稿件六个主题得分中，恰好一个主题达到阈值 → 建议归档到该主题；
零个或多个主题达到阈值 → 不给出归档结论，提示人工复核。
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .articles_data import ARTICLES, TOPICS
from .tfidf_model import get_model

# 校准要求：留出文章真主题得分达到阈值的比例不得低于该值
REQUIRED_COVERAGE = 0.9


# ---------------- 主题评分 ----------------


def score_topics(text: str) -> list[dict]:
    """稿件对每个主题的得分（最高余弦相似度）与依据文章。

    返回按 TOPICS 顺序的列表，每项含 topic/score/evidence。
    """
    model = get_model()
    q = model.transform_interest(text)
    sims = cosine_similarity(q, model.article_matrix)[0]
    out: list[dict] = []
    for topic in TOPICS:
        best_sim = -1.0
        best_article: dict | None = None
        for a, s in zip(ARTICLES, sims):
            if a["topic"] != topic:
                continue
            if float(s) > best_sim:
                best_sim = float(s)
                best_article = a
        score = max(0.0, min(1.0, best_sim))  # 显式截断到 [0,1]
        out.append(
            {
                "topic": topic,
                "score": score,
                "evidence": {
                    "article_id": best_article["id"],
                    "title": best_article["title"],
                    "author": best_article["author"],
                    "similarity": score,
                },
            }
        )
    return out


def decide(topic_scores: list[dict], threshold: float) -> dict:
    """归档决策：恰好一个主题达到阈值才建议归档，否则人工复核。"""
    for t in topic_scores:
        t["reaches_threshold"] = t["score"] >= threshold
    reached = [t for t in topic_scores if t["reaches_threshold"]]
    if len(reached) == 1:
        return {
            "action": "archive",
            "topic": reached[0]["topic"],
            "reason": f"仅「{reached[0]['topic']}」达到校准阈值",
        }
    if not reached:
        return {
            "action": "review",
            "topic": None,
            "reason": "没有任何主题达到校准阈值，建议人工复核",
        }
    names = "、".join(t["topic"] for t in reached)
    return {
        "action": "review",
        "topic": None,
        "reason": f"多个主题达到校准阈值（{names}），建议人工复核",
    }


# ---------------- 按作者整组留出校准 ----------------


@dataclass
class Calibration:
    threshold: float                 # 校准出的最高有效阈值
    required_coverage: float         # 要求的最低覆盖率（0.9）
    coverage: float                  # 该阈值下的实际留出覆盖率
    folds: int                       # 折数（= 作者数）
    held_out: int                    # 留出文章总数
    true_scores_desc: list[float] = field(default_factory=list)  # 留出真主题得分（降序）
    fold_details: list[dict] = field(default_factory=list)       # 每折诊断信息

    def summary(self) -> dict:
        return {
            "threshold": self.threshold,
            "required_coverage": self.required_coverage,
            "coverage": self.coverage,
            "folds": self.folds,
            "held_out": self.held_out,
            "fold_details": self.fold_details,
        }


def _new_vectorizer() -> TfidfVectorizer:
    """与生产模型完全一致的超参数；校准每折都新建实例、重新拟合。"""
    return TfidfVectorizer(
        analyzer="char",
        ngram_range=(2, 3),
        min_df=1,
        sublinear_tf=True,
        norm="l2",
    )


def run_calibration() -> Calibration:
    """按作者整组留出校准阈值（每折重新拟合特征）。"""
    authors = sorted({a["author"] for a in ARTICLES})
    true_scores: list[float] = []
    fold_details: list[dict] = []
    for held_out_author in authors:
        train = [a for a in ARTICLES if a["author"] != held_out_author]
        held = [a for a in ARTICLES if a["author"] == held_out_author]
        vec = _new_vectorizer()
        # 关键：特征（词表与 IDF）在每折的训练子集上重新拟合
        train_matrix = vec.fit_transform([a["body"] for a in train])
        fold_scores: list[float] = []
        for art in held:
            q = vec.transform([art["body"]])
            sims = cosine_similarity(q, train_matrix)[0]
            same_topic = [
                float(s) for s, t in zip(sims, train) if t["topic"] == art["topic"]
            ]
            fold_scores.append(max(0.0, min(1.0, max(same_topic))))
        true_scores.extend(fold_scores)
        fold_details.append(
            {
                "held_out_author": held_out_author,
                "n_train": len(train),
                "n_held_out": len(held),
                "n_features": len(vec.vocabulary_),
                "true_scores": fold_scores,
            }
        )

    n = len(true_scores)
    # 至少 90%：需达标篇数 = ceil(n * 0.9)（减 1e-9 抵消浮点误差，30*0.9 应为 27）
    need = math.ceil(n * REQUIRED_COVERAGE - 1e-9)
    ranked = sorted(true_scores, reverse=True)
    # 满足“≥ need 篇达标”的最高阈值即第 need 大的留出真主题得分
    threshold = ranked[need - 1] if n else 0.0
    coverage = sum(1 for s in true_scores if s >= threshold) / n if n else 0.0
    return Calibration(
        threshold=threshold,
        required_coverage=REQUIRED_COVERAGE,
        coverage=coverage,
        folds=len(authors),
        held_out=n,
        true_scores_desc=ranked,
        fold_details=fold_details,
    )


_calibration: Calibration | None = None
_calibration_lock = threading.Lock()


def get_calibration() -> Calibration:
    """进程内单例，避免重复校准。"""
    global _calibration
    if _calibration is None:
        with _calibration_lock:
            if _calibration is None:
                _calibration = run_calibration()
    return _calibration


# ---------------- 完整判定 ----------------


def classify(text: str) -> dict:
    """对稿件正文做完整归档判定：评分 + 校准阈值 + 决策。"""
    cal = get_calibration()
    topics = score_topics(text)
    decision = decide(topics, cal.threshold)
    return {
        "threshold": cal.threshold,
        "topics": topics,
        "decision": decision,
        "text_length": len(text),
        "text_preview": text[:80],
    }

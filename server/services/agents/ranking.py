"""Bounded, relevance-ranked roster (task 2.4, flag TOPK_ROSTER; dormancy 2.5).

Instead of every agent ever created (M1: the roster block grows linearly,
~24.7k tokens est. at 2,000 agents), the IA sees K agents: the ``hot`` most
recently active ones plus the best ``K - hot`` by BM25 against the current
turn's text. BM25 parameters are the textbook defaults (k1 = 1.2, b = 0.75),
not tuned. Tokenizer and stopwords are shared with the resolver.

Agents not shown are still reachable: the resolver matches names, aliases and
identifiers across ALL agents, including dormant ones.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Dict, List, Optional, Sequence

from ...overload_config import BM25_B, BM25_K1, TOPK_HOT, TOPK_K
from .registry import AgentRow, parse_time
from .resolver import tokens


def _recency(row: AgentRow) -> float:
    last = parse_time(row.last_active_at) or parse_time(row.created_at)
    return last.timestamp() if last else 0.0


def bm25_scores(query: str, documents: Dict[int, str], *, k1: Optional[float] = None, b: Optional[float] = None) -> Dict[int, float]:
    """Okapi BM25 of ``query`` against each document (pure Python)."""
    k1 = float(BM25_K1.get()) if k1 is None else k1
    b = float(BM25_B.get()) if b is None else b
    tokenized = {key: tokens(text) for key, text in documents.items()}
    n = len(tokenized)
    if n == 0:
        return {}
    avgdl = sum(len(t) for t in tokenized.values()) / n or 1.0
    df: Counter = Counter()
    for words in tokenized.values():
        df.update(set(words))
    query_terms = tokens(query)
    scores: Dict[int, float] = {}
    for key, words in tokenized.items():
        tf = Counter(words)
        score = 0.0
        for term in query_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            freq = tf[term]
            score += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * len(words) / avgdl))
        scores[key] = score
    return scores


def select(query: str, agents: Sequence[AgentRow], documents: Dict[int, str], *, k: Optional[int] = None,
           hot: Optional[int] = None) -> List[AgentRow]:
    """``hot`` most recent agents + top ``k - hot`` by BM25; ties broken by recency, then name.

    An empty query (or no matching terms) falls back to recency only.
    """
    k = int(TOPK_K.get()) if k is None else k
    hot = int(TOPK_HOT.get()) if hot is None else hot
    by_recency = sorted(agents, key=lambda r: (-_recency(r), r.name))
    if len(agents) <= k:
        return by_recency
    chosen = by_recency[:hot]
    chosen_ids = {r.id for r in chosen}
    scores = bm25_scores(query, {r.id: documents.get(r.id, r.name) for r in agents if r.id not in chosen_ids})
    ranked = sorted(
        (r for r in agents if r.id not in chosen_ids),
        key=lambda r: (-scores.get(r.id, 0.0), -_recency(r), r.name),
    )
    return chosen + ranked[: max(0, k - hot)]


__all__ = ["bm25_scores", "select"]

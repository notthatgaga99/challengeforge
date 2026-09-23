"""Retrieval evaluation metrics.

Definitions (binary relevance, query q with relevant set R, ranked list L[1..K]):

Recall@K
    |{{ d ∈ R : d appears in L[1..K] }}| / |R|
    If |R|=0, undefined (skipped).

Precision@K
    |{{ d ∈ L[1..K] : d ∈ R }}| / K

MRR (Mean Reciprocal Rank)
    For one query: 1 / rank_of_first_relevant, or 0 if none in the list.
    Macro-average across queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable, Sequence
from uuid import UUID


@dataclass(frozen=True)
class QueryMetrics:
    query_id: str
    recall_at: dict[int, float]
    precision_at: dict[int, float]
    mrr: float
    first_relevant_rank: int | None
    zero_hit: bool
    retrieved_relevant: int


def recall_at_k(
    ranked_ids: Sequence[Hashable], relevant: set[Hashable], k: int
) -> float:
    if not relevant:
        raise ValueError("relevant set must be non-empty for Recall@K")
    top = set(ranked_ids[:k])
    return len(top & relevant) / len(relevant)


def precision_at_k(
    ranked_ids: Sequence[Hashable], relevant: set[Hashable], k: int
) -> float:
    if k < 1:
        raise ValueError("k must be >= 1")
    top = ranked_ids[:k]
    return sum(1 for x in top if x in relevant) / k


def reciprocal_rank(
    ranked_ids: Sequence[Hashable], relevant: set[Hashable]
) -> float:
    for i, item in enumerate(ranked_ids, start=1):
        if item in relevant:
            return 1.0 / i
    return 0.0


def first_relevant_rank(
    ranked_ids: Sequence[Hashable], relevant: set[Hashable]
) -> int | None:
    for i, item in enumerate(ranked_ids, start=1):
        if item in relevant:
            return i
    return None


def evaluate_query(
    *,
    query_id: str,
    ranked_ids: Sequence[Hashable],
    relevant: set[Hashable],
    ks: Iterable[int] = (1, 3, 5, 10),
) -> QueryMetrics:
    ks_list = list(ks)
    if not relevant:
        raise ValueError(f"query {query_id} has empty relevant set")
    recall = {k: recall_at_k(ranked_ids, relevant, k) for k in ks_list}
    precision = {k: precision_at_k(ranked_ids, relevant, k) for k in ks_list}
    rr = reciprocal_rank(ranked_ids, relevant)
    fr = first_relevant_rank(ranked_ids, relevant)
    retrieved = len(set(ranked_ids) & relevant)
    return QueryMetrics(
        query_id=query_id,
        recall_at=recall,
        precision_at=precision,
        mrr=rr,
        first_relevant_rank=fr,
        zero_hit=fr is None,
        retrieved_relevant=retrieved,
    )


def macro_average(metrics: Sequence[QueryMetrics], ks: Sequence[int]) -> dict:
    if not metrics:
        return {
            "n_queries": 0,
            "recall_at": {k: None for k in ks},
            "precision_at": {k: None for k in ks},
            "mrr": None,
            "zero_hit_rate": None,
        }
    n = len(metrics)
    return {
        "n_queries": n,
        "recall_at": {
            k: round(sum(m.recall_at[k] for m in metrics) / n, 4) for k in ks
        },
        "precision_at": {
            k: round(sum(m.precision_at[k] for m in metrics) / n, 4) for k in ks
        },
        "mrr": round(sum(m.mrr for m in metrics) / n, 4),
        "zero_hit_rate": round(sum(1 for m in metrics if m.zero_hit) / n, 4),
    }


def as_id_set(ids: Iterable[UUID | str]) -> set[str]:
    return {str(x) for x in ids}

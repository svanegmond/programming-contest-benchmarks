"""Unbiased pass@k (Chen et al. 2021) and mean partial scores."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from cco_eval.run_records import RunRecord


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased estimator of pass@k from Chen et al. 2021.

    pass@k = 1 - C(n-c, k) / C(n, k), with the usual conventions that the
    estimator is 1 when n-c < k and 0 when n == 0.
    """
    if n <= 0 or k <= 0:
        return 0.0
    k = min(k, n)
    if c < 0:
        c = 0
    if c > n:
        c = n
    if n - c < k:
        return 1.0
    estimate = 1.0
    for i in range(k):
        estimate *= (n - c - i) / (n - i)
    return 1.0 - estimate


def _language(record: RunRecord) -> str:
    language = record.attempt.language
    return str(language) if language else "none"


def _success(record: RunRecord) -> bool:
    return bool(record.verdict.all_pass)


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def summarize_groups(records: list[RunRecord], k: int) -> list[dict[str, object]]:
    """One row per (model, language) plus an all-languages row per model.

    Problems missing a language are omitted from that language's row; scores
    are never filled in from a different language.
    """
    rows: list[dict[str, object]] = []
    by_model: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        by_model[record.attempt.model].append(record)
    for model in sorted(by_model):
        group = by_model[model]
        rows.append(_group_row(model, "all", group, k))
        by_language: dict[str, list[RunRecord]] = defaultdict(list)
        for record in group:
            by_language[_language(record)].append(record)
        for language in sorted(by_language):
            rows.append(_group_row(model, language, by_language[language], k))
    return rows


def _group_row(model: str, language: str, records: list[RunRecord], k: int) -> dict[str, object]:
    by_problem: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        by_problem[record.attempt.problem_id].append(record)
    pass_scores: list[float] = []
    partials: list[float] = []
    on_time = over_time = truncated = fail = 0
    for group in by_problem.values():
        n = len(group)
        c = sum(1 for record in group if _success(record))
        pass_scores.append(pass_at_k(n, c, k))
        partials.append(_mean(record.verdict.partial_score for record in group))
        for record in group:
            kind = record.attempt.failure_kind
            if kind is not None and str(kind) == "truncated":
                truncated += 1
            elif str(record.verdict.outcome) == "pass_on_time":
                on_time += 1
            elif str(record.verdict.outcome) == "pass_over_time":
                over_time += 1
            else:
                fail += 1
    return {
        "model": model,
        "language": language,
        "problems": len(by_problem),
        "attempts": len(records),
        "pass_at_k": _mean(pass_scores),
        "mean_partial_score": _mean(partials),
        "on_time": on_time,
        "over_time": over_time,
        "truncated": truncated,
        "fail": fail,
        "k": k,
    }

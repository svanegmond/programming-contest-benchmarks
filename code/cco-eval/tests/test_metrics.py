from cco_eval.metrics import pass_at_k, summarize_groups
from cco_eval.waterloo import Attempt, Mode, Outcome3, Verdict
from cco_eval.run_records import RunRecord


def test_unbiased_pass_at_k_matches_chen_et_al_special_cases():
    assert pass_at_k(1, 1, 1) == 1.0
    assert pass_at_k(1, 0, 1) == 0.0
    assert pass_at_k(2, 1, 1) == 0.5
    assert pass_at_k(2, 1, 2) == 1.0
    assert pass_at_k(5, 0, 3) == 0.0
    assert pass_at_k(5, 5, 3) == 1.0
    assert abs(pass_at_k(10, 1, 1) - 0.1) < 1e-12


def _record(problem: str, language: str | None, outcome: Outcome3, partial: float, sample: int = 1) -> RunRecord:
    attempt = Attempt(
        f"{problem}-{sample}", problem, Mode.SINGLE_SHOT, "m", "openrouter", language,
        "env", "hash", "t", None, sample,
    )
    verdict = Verdict(attempt.id, problem, {}, outcome != Outcome3.FAIL, partial, outcome, "env")
    return RunRecord(attempt, verdict, sample)


def test_pass_at_k_counts_correct_output_even_when_over_the_statement_limit():
    records = [_record("p1", "cpp", Outcome3.PASS_OVER_TIME, 1.0)]
    row = summarize_groups(records, 1)[0]
    assert row["pass_at_k"] == 1.0
    assert row["over_time"] == 1
    assert row["on_time"] == 0
    assert row["fail"] == 0


def test_summary_splits_by_language_and_does_not_take_the_better_language():
    records = [
        _record("p1", "cpp", Outcome3.PASS_ON_TIME, 1.0),
        _record("p1", "python", Outcome3.FAIL, 0.0),
        _record("p2", "python", Outcome3.FAIL, 0.2),
    ]
    rows = { (row["model"], row["language"]): row for row in summarize_groups(records, 1) }
    assert rows[("m", "cpp")]["pass_at_k"] == 1.0
    assert rows[("m", "python")]["pass_at_k"] == 0.0
    assert rows[("m", "all")]["problems"] == 2
    assert rows[("m", "cpp")]["problems"] == 1
    assert rows[("m", "python")]["mean_partial_score"] == 0.1

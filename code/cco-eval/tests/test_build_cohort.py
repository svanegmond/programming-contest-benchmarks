import json
from pathlib import Path

from build_cohort import draw


def _problem(root: Path, problem_id: str, status: str, reason: str | None, kind: str) -> None:
    directory = root / problem_id
    directory.mkdir(parents=True)
    (directory / "status.json").write_text(json.dumps({"problem_id": problem_id, "status": status, "reason": reason}))
    (directory / "problem.json").write_text(json.dumps({"grading_kind": kind}))


def test_cohort_is_seeded_and_draws_from_the_right_pools(tmp_path):
    for index in range(12):
        _problem(tmp_path, f"ok-{index:02d}", "admitted", None, "exact" if index % 2 else "float_tol")
    _problem(tmp_path, "v-1", "rejected", "needs_validator", "validator")
    _problem(tmp_path, "s-1", "rejected", "needs_scorer", "scorer")
    _problem(tmp_path, "u-1", "rejected", "unclear", "exact")
    _problem(tmp_path, "i-1", "rejected", "interactive", "interactive")

    first = draw(tmp_path, 7, 10, 2)

    assert first == draw(tmp_path, 7, 10, 2)
    assert first != draw(tmp_path, 8, 10, 2)
    assert len(first["deterministic"]) == 10
    assert set(first["deterministic"]) <= {f"ok-{index:02d}" for index in range(12)}
    assert first["unusual"] == ["s-1", "v-1"]

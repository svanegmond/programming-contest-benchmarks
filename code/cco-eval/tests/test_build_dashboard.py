import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_dashboard import _records, metric_line, render_dashboard


def record(problem_id: str, state: str, reason: str | None, year: int) -> dict:
    return {
        "id": problem_id,
        "status": {"status": state, "reason": reason, "worker_model": None},
        "problem": {
            "year": year,
            "title": "Test | title",
            "grading_kind": "exact",
            "subtasks": [{"name": "full_constraints", "weight": 1.0}],
            "time_limit_s": 1.0,
        },
        "test_count": 3,
        "directory": Path("problems") / problem_id,
    }


def test_metric_counts_rejection_reasons_and_delta():
    rows = [
        record("cco-2024-d1p1", "admitted", None, 2024),
        record("cco-2024-d1p2", "rejected", "needs_validator", 2024),
        record("cco-2024-d1p3", "pending", "unclear", 2024),
        record("cco-2024-d2p1", "rejected", "needs_validator", 2024),
    ]
    assert metric_line(rows, 2) == "waterloo-suite admitted=1 rejected=2 (needs_validator=2) pending=1 delta_vs_main=-1"


def test_dashboard_has_every_problem_and_escapes_table_cells():
    rendered = render_dashboard([record("cco-2024-d1p1", "admitted", None, 2024)])
    assert "| cco-2024-d1p1 | 2024 | Test \\| title | admitted |" in rendered
    assert "| Grading kind | Subtasks | Tests | Time limit (s) | Worker model | Notes |" in rendered


def test_notes_column_links_notes_and_shows_sample_conflict(tmp_path: Path):
    problem_id = "cco-2024-d1p1"
    problem_dir = tmp_path / problem_id
    problem_dir.mkdir()
    (problem_dir / "notes.md").write_text("Worker notes", encoding="utf-8")
    row = record(problem_id, "admitted", None, 2024)
    row["directory"] = problem_dir
    row["sample_conflict"] = "worker sample did not match"

    rendered = render_dashboard([row])

    assert f"[{problem_id}/notes.md](problems/{problem_id}/notes.md); sample conflict: worker sample did not match" in rendered


def test_records_load_sample_conflicts_from_admission_report(tmp_path: Path):
    problem_id = "cco-2024-d1p1"
    problem_dir = tmp_path / problem_id
    problem_dir.mkdir()
    (problem_dir / "status.json").write_text('{"problem_id":"cco-2024-d1p1","status":"admitted"}', encoding="utf-8")
    (problem_dir / "problem.json").write_text('{"year":2024,"title":"Demo"}', encoding="utf-8")
    (tmp_path / "admission-report.json").write_text(
        '{"sample_conflicts":[{"problem_id":"cco-2024-d1p1","detail":"sample mismatch"}]}',
        encoding="utf-8",
    )

    rows = _records(tmp_path)

    assert rows[0]["sample_conflict"] == "sample mismatch"
    assert "sample conflict: sample mismatch" in render_dashboard(rows)

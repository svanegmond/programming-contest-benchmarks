import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS = ROOT / "env-study/problem-requirements.jsonl"


def rows() -> list[dict]:
    return [json.loads(line) for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()]


def test_every_row_has_the_same_fields_and_a_problem_directory():
    all_rows = rows()
    keys = set(all_rows[0])
    assert all(set(row) == keys for row in all_rows)
    assert all((ROOT / "problems" / row["id"] / "status.json").is_file() for row in all_rows)


def test_2015_rows_cover_all_six_problems_and_marks_sum_to_25():
    rows_2015 = [row for row in rows() if row["year"] == 2015]
    assert [row["id"] for row in rows_2015] == [f"cco-2015-d{d}p{p}" for d in (1, 2) for p in (1, 2, 3)]
    for row in rows_2015:
        assert row["subtask_marks_total"] + row["implied_full_constraint_marks_inferred"] == 25

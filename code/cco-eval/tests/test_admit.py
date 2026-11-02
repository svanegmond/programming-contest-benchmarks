import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admit import (
    admit_row,
    discover_pairs,
    extract_samples,
    SAMPLE_CONFLICT_DETAIL,
    load_dmoj_limits,
    run_admission,
    sample_matches,
    _subtask_for,
    _weights,
)


HTML = """<!doctype html><html><body>
<h2>Day 1, Problem 1: Demo Problem</h2>
<p>Time Limit: 1 seconds</p>
<h3>Sample Input 1</h3><pre><code>4\n</code></pre>
<h4>Output for Sample Input 1</h4><pre><code>8\n</code></pre>
</body></html>"""
STATEMENT_PATH = "statements/2024/demo.html"
INPUT_MEMBER = "day1data/d1p1/d1p1.sample-001.in"
OUTPUT_MEMBER = "day1data/d1p1/d1p1.sample-001.out"


def requirement() -> dict[str, object]:
    return {
        "id": "cco-2024-d1p1",
        "year": 2024,
        "day": 1,
        "title": "Demo Problem",
        "source_html": STATEMENT_PATH,
        "time_limit_seconds": 1.0,
        "memory_limit_stated": "unknown",
        "io_mode": "stdin/stdout",
        "checker_class": "exact",
        "subtasks": [{"marks": 10}, {"marks": 15}],
        "implied_full_constraint_marks_inferred": None,
    }


def make_fixture(tmp_path: Path, expected: bytes = b"8\n") -> tuple[Path, Path, Path, dict[str, object]]:
    corpus = tmp_path / "statements"
    html_path = tmp_path / STATEMENT_PATH
    html_path.parent.mkdir(parents=True)
    html_path.write_text(HTML, encoding="utf-8")
    zips_root = tmp_path / "zips"
    year_dir = zips_root / "2024"
    year_dir.mkdir(parents=True)
    zip_path = year_dir / "data.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(INPUT_MEMBER, b"4\n")
        zf.writestr(OUTPUT_MEMBER, expected)
        zf.writestr("day1data/d1p1/d1p1.1-001.in", b"1\n")
        zf.writestr("day1data/d1p1/d1p1.1-001.out", b"2\n")
        zf.writestr("day1data/d1p1/d1p1.2-002.in", b"2\n")
        zf.writestr("day1data/d1p1/d1p1.2-002.out", b"4\n")
    return zips_root, corpus, zip_path, requirement()


@pytest.mark.parametrize(
    "members,row,expected_layout",
    [
        (["day1data/d1p1/d1p1.1-001.in", "day1data/d1p1/d1p1.1-001.out"], requirement(), "dNpM_filename"),
        (["day1data/demo_problem/input.in", "day1data/demo_problem/input.out"], requirement(), "day_directory_title"),
        (["day1data/prob1/input.in", "day1data/prob1/input.out"], requirement(), "day_directory_problem_number"),
        (
            ["day2/pronet/04.in", "day2/pronet/04.a"],
            {**requirement(), "id": "cco-2017-d2p2", "day": 2},
            "day_directory_title",
        ),
    ],
)
def test_layout_recognizers_on_synthetic_archives(tmp_path: Path, members, row, expected_layout):
    archive = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for member in members:
            zf.writestr(member, "test")
    with zipfile.ZipFile(archive) as zf:
        layout, matches = __import__("admit")._recognized_members(zf, row)
    assert layout == expected_layout
    assert len(matches) == 2


def test_sample_matching_uses_both_input_and_expected_output():
    html = HTML
    samples = extract_samples(html, 1, 1)
    assert samples == [("4", "8")]
    pairs = [
        {"input_name": "wrong.in", "input_text": "4", "expected_text": "9"},
        {"input_name": "right.in", "input_text": " 4\n", "expected_text": "8\n"},
    ]
    assert sample_matches(samples, pairs) == "right.in"


def test_misnumbered_sample_member_is_found_inside_problem_directory(tmp_path: Path):
    archive = tmp_path / "misnumbered-sample.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("day1data/cco_d1p1/d1p1.1-001.in", "1\n")
        zf.writestr("day1data/cco_d1p1/d1p1.1-001.out", "2\n")
        zf.writestr("day1data/cco_d1p1/d2p1.sample.in", "4\n")
        zf.writestr("day1data/cco_d1p1/d2p1.sample.out", "8\n")
    with zipfile.ZipFile(archive) as zf:
        _, members = __import__("admit")._recognized_members(zf, requirement())
    pairs = [member.filename for member in members]
    assert "day1data/cco_d1p1/d2p1.sample.in" in pairs
    assert "day1data/cco_d1p1/d2p1.sample.out" in pairs


def test_day_title_recognizer_does_not_match_title_word_inside_another_directory(tmp_path: Path):
    row = {
        **requirement(),
        "id": "cco-2017-d1p3",
        "year": 2017,
        "day": 1,
        "title": "Vera and Modern Art",
    }
    archive = tmp_path / "collision.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("day1/cartesian/01.in", "not the art problem")
        zf.writestr("day1/cartesian/01.out", "not the art problem")
        zf.writestr("day1/art/02.in", "the art problem")
        zf.writestr("day1/art/02.out", "the art problem")

    with zipfile.ZipFile(archive) as zf:
        layout, recognized = __import__("admit")._recognized_members(zf, row)

    assert layout == "day_directory_title"
    assert {member.filename for member in recognized} == {"day1/art/02.in", "day1/art/02.out"}


def test_admission_output_is_byte_deterministic(tmp_path: Path):
    zips_root, corpus, _, row = make_fixture(tmp_path)
    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(json.dumps(row) + "\n", encoding="utf-8")
    first, second = tmp_path / "first", tmp_path / "second"
    run_admission(zips_root, corpus, requirements, first)
    run_admission(zips_root, corpus, requirements, second)
    for name in ("problem.json", "tests.jsonl", "status.json"):
        assert (first / row["id"] / name).read_bytes() == (second / row["id"] / name).read_bytes()
    tests = [json.loads(line) for line in (first / row["id"] / "tests.jsonl").read_text().splitlines()]
    assert len(tests) == 2
    assert {test["subtask"] for test in tests} == {"subtask_1", "subtask_2"}


def _write_subtask_map(out: Path, row: dict[str, object], *, missing_member: bool = False, unknown_subtask: bool = False) -> Path:
    problem_dir = out / str(row["id"])
    problem_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "problem_id": row["id"],
        "zip": "2024/data.zip",
        "method": "Synthetic test groups map to their numbered subtasks.",
        "members": [
            {
                "input": "day1data/d1p1/d1p1.1-001.in",
                "expected": "day1data/d1p1/d1p1.1-001.out",
                "subtask": "unknown" if unknown_subtask else "subtask_1",
            },
            {
                "input": "day1data/d1p1/d1p1.2-002.in",
                "expected": "day1data/d1p1/missing.out" if missing_member else "day1data/d1p1/d1p1.2-002.out",
                "subtask": "subtask_2",
            },
        ],
        "excluded": [{"member": INPUT_MEMBER, "why": "sample"}],
    }
    path = problem_dir / "subtask_map.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def test_subtask_map_admits_and_is_byte_deterministic(tmp_path: Path):
    zips_root, corpus, _, row = make_fixture(tmp_path)
    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    _write_subtask_map(out, row)

    run_admission(zips_root, corpus, requirements, out)
    problem_dir = out / str(row["id"])
    first_tests = (problem_dir / "tests.jsonl").read_bytes()
    first_status = (problem_dir / "status.json").read_bytes()
    run_admission(zips_root, corpus, requirements, out)

    status = json.loads((problem_dir / "status.json").read_text(encoding="utf-8"))
    tests = [json.loads(line) for line in (problem_dir / "tests.jsonl").read_text(encoding="utf-8").splitlines()]
    assert status["status"] == "admitted"
    assert status["reason"] is None
    assert len(tests) == 2
    assert tests[0]["input_path"].startswith("2024/data.zip::")
    assert (problem_dir / "tests.jsonl").read_bytes() == first_tests
    assert (problem_dir / "status.json").read_bytes() == first_status


@pytest.mark.parametrize(
    "map_kwargs,rule",
    [
        ({"missing_member": True}, "missing_zip_member"),
        ({"unknown_subtask": True}, "unknown_subtask"),
    ],
)
def test_invalid_subtask_map_is_rejected_with_rule(tmp_path: Path, map_kwargs, rule: str):
    zips_root, corpus, _, row = make_fixture(tmp_path)
    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    _write_subtask_map(out, row, **map_kwargs)

    run_admission(zips_root, corpus, requirements, out)

    status = json.loads((out / str(row["id"]) / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "rejected"
    assert status["reason"] == "test_layout_unrecognized"
    assert any(rule in item for item in status["evidence"])


def test_worker_admission_without_matching_sample_is_rejected_and_reported(tmp_path: Path):
    zips_root, corpus, source_zip, row = make_fixture(tmp_path)
    corrupt_zip = source_zip.with_name("corrupt.zip")
    with zipfile.ZipFile(source_zip) as source, zipfile.ZipFile(corrupt_zip, "w") as target:
        for info in source.infolist():
            contents = b"wrong\n" if info.filename == OUTPUT_MEMBER else source.read(info)
            target.writestr(info, contents)
    source_zip.unlink()
    corrupt_zip.rename(source_zip)
    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    _write_subtask_map(out, row)
    status = {
        "problem_id": row["id"],
        "status": "admitted",
        "reason": None,
        "evidence": ["worker evidence"],
        "worker_model": "synthetic-worker",
        "checked_at": "2026-10-20T00:00:00Z",
    }
    status_path = out / str(row["id"]) / "status.json"
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

    run_admission(zips_root, corpus, requirements, out)

    rewritten = json.loads(status_path.read_text(encoding="utf-8"))
    assert rewritten["status"] == "rejected"
    assert rewritten["reason"] == "samples_disagree"
    assert rewritten["worker_model"] == "synthetic-worker"
    assert rewritten["evidence"][0] == "worker evidence"
    expected_report = [{"problem_id": row["id"], "detail": SAMPLE_CONFLICT_DETAIL}]
    report_path = out / "admission-report.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["sample_conflicts"] == expected_report

    first = (status_path.read_bytes(), report_path.read_bytes())
    run_admission(zips_root, corpus, requirements, out)
    assert (status_path.read_bytes(), report_path.read_bytes()) == first


def test_worker_admitted_problem_without_map_fails_loudly(tmp_path: Path):
    zips_root, corpus, _, row = make_fixture(tmp_path)
    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    problem_dir = out / str(row["id"])
    problem_dir.mkdir(parents=True)
    (problem_dir / "status.json").write_text(
        json.dumps({"status": "admitted", "worker_model": "synthetic-worker"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="has no subtask_map.json"):
        run_admission(zips_root, corpus, requirements, out)


def test_corrupted_sample_output_is_rejected_as_samples_disagree(tmp_path: Path):
    zips_root, corpus, zip_path, row = make_fixture(tmp_path)
    corrupt_zip = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(zip_path) as source, zipfile.ZipFile(corrupt_zip, "w") as target:
        for info in source.infolist():
            contents = b"999\n" if info.filename == OUTPUT_MEMBER else source.read(info)
            target.writestr(info, contents)
    corrupt_root = tmp_path / "corrupt-zips/2024"
    corrupt_root.mkdir(parents=True)
    corrupt_zip.rename(corrupt_root / "data.zip")
    _, _, status = admit_row(row, tmp_path / "corrupt-zips", corpus, "2026-10-20T00:00:00Z")
    assert status.status == "rejected"
    assert status.reason == "samples_disagree"


def test_missing_zip_is_rejected_as_test_data_missing(tmp_path: Path):
    zips_root, corpus, zip_path, row = make_fixture(tmp_path)
    zip_path.unlink()
    _, _, status = admit_row(row, zips_root, corpus, "2026-10-20T00:00:00Z")
    assert status.status == "rejected"
    assert status.reason == "test_data_missing"


def test_subtask_weights_include_inferred_full_constraint_marks():
    row = requirement()
    row["subtasks"] = [{"marks": 5}]
    row["implied_full_constraint_marks_inferred"] = 20
    subtasks = _weights(row)
    assert [(item.name, item.weight) for item in subtasks] == [("subtask_1", 0.2), ("full_constraints", 0.8)]


def test_subtask_groups_use_ordered_study_weights():
    row = requirement()
    assert _subtask_for("day1data/problem/demo.2-01", row) == "subtask_2"
    assert _subtask_for("day1data/problem/s1/demo.01", row) == "subtask_1"
    row["subtasks"] = [{"marks": 5}, {"marks": 10}]
    row["implied_full_constraint_marks_inferred"] = 10
    assert _subtask_for("day1data/problem/s3/demo.01", row) == "full_constraints"


def _dmoj_limits_file(path: Path, row: dict[str, object], mb: int | None = 1024) -> Path:
    path.write_text(
        json.dumps(
            {
                "problems": [
                    {
                        "problem_id": row["id"],
                        "dmoj_code": "cco24p1" if mb is not None else None,
                        "memory_limit_mb": mb,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_dmoj_limit_fills_missing_statement_limit_and_reruns_are_identical(tmp_path: Path):
    zips_root, corpus, _, row = make_fixture(tmp_path)
    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(json.dumps(row) + "\n", encoding="utf-8")
    limits = _dmoj_limits_file(tmp_path / "dmoj-memory-limits.json", row)
    out = tmp_path / "out"
    _write_subtask_map(out, row)
    (out / str(row["id"]) / "status.json").write_text(
        json.dumps({"status": "admitted", "reason": None, "evidence": [], "worker_model": "w"}), encoding="utf-8"
    )

    run_admission(zips_root, corpus, requirements, out, dmoj_limits_path=limits)
    problem_path = out / str(row["id"]) / "problem.json"
    first = problem_path.read_bytes()
    problem = json.loads(first)
    assert problem["memory_limit_mb"] == 1024
    assert problem["memory_limit_source"] == "dmoj:cco24p1"

    run_admission(zips_root, corpus, requirements, out, dmoj_limits_path=limits)
    assert problem_path.read_bytes() == first


def test_statement_memory_limit_wins_over_dmoj(tmp_path: Path):
    zips_root, corpus, _, row = make_fixture(tmp_path)
    html_path = tmp_path / STATEMENT_PATH
    html_path.write_text(HTML.replace("Time Limit: 1 seconds", "Time Limit: 1 seconds Memory Limit: 256 MB"), encoding="utf-8")
    limits = _dmoj_limits_file(tmp_path / "limits.json", row)
    problem, _, _ = admit_row(row, zips_root, corpus, "2026-10-20T00:00:00Z", load_dmoj_limits(limits))
    assert problem.memory_limit_mb == 256
    assert problem.memory_limit_source == STATEMENT_PATH


def test_unmatched_dmoj_entry_leaves_memory_limit_null(tmp_path: Path):
    zips_root, corpus, _, row = make_fixture(tmp_path)
    limits = _dmoj_limits_file(tmp_path / "limits.json", row, mb=None)
    problem, _, _ = admit_row(row, zips_root, corpus, "2026-10-20T00:00:00Z", load_dmoj_limits(limits))
    assert problem.memory_limit_mb is None
    assert problem.memory_limit_source is None

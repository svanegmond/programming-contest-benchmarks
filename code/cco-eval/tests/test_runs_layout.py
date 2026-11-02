import csv
import json
import re
from pathlib import Path

import export_run

ROOT = Path(__file__).resolve().parents[3]
RUNS = sorted(p for p in (ROOT / "runs").iterdir() if p.is_dir())
OUTCOMES = {"pass_on_time", "pass_over_time", "truncated", "fail"}


def test_there_is_at_least_one_published_run():
    assert RUNS


def test_every_run_folder_holds_exactly_the_three_publishable_files():
    for run in RUNS:
        assert sorted(p.name for p in run.iterdir()) == ["manifest.json", "results.csv", "summary.md"], run


def test_every_manifest_has_the_agreed_keys_and_no_hashes():
    for run in RUNS:
        manifest = json.loads((run / "manifest.json").read_text())
        assert set(manifest) - {"note"} == {"run_id", "label", *export_run.MANIFEST_FIELDS}, run
        assert not re.search(r"[0-9a-f]{40,}", "".join(f.read_text() for f in run.iterdir())), run
        assert manifest["label"] != "test" or run.name.endswith("-test"), run


def test_every_results_file_has_the_agreed_columns_and_known_outcomes():
    for run in RUNS:
        manifest = json.loads((run / "manifest.json").read_text())
        rows = list(csv.DictReader((run / "results.csv").read_text().splitlines()))
        assert tuple(rows[0]) == export_run.CSV_FIELDS, run
        assert {r["outcome"] for r in rows} <= OUTCOMES, run
        assert {r["verdict"] for r in rows} <= {"pass", "fail"}, run
        assert {r["model"] for r in rows} <= set(manifest["models"]), run
        assert {r["problem"] for r in rows} <= set(manifest["problem_ids"]), run

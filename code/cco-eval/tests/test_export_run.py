import csv
import json
import shutil
from pathlib import Path

import export_run

FIXTURE = Path(__file__).parent / "fixtures/waterloo/run"


def make_run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    shutil.copytree(FIXTURE, run)
    (run / "run.json").write_text(json.dumps({
        "run_id": "r1", "git_sha": "a" * 40, "env_id": "arch-fixture-2026-10-20", "cohort_hash": "b" * 64,
        "models": ["fixture-model"], "mode": "agentic", "caps": {"backend": "vm"}, "budget": None,
        "problem_ids": ["fixture-2024-d1p1"], "timeout_s": 7200,
        "judge": {"ssh_host": "user@private-host"},
        "k": 1, "temperature": 0.0, "reasoning_effort": "high", "lane": "cpp",
        "selection": "pass_at_k", "prompt_version": "cco-v2",
    }))
    (run / "telemetry.jsonl").write_text(
        json.dumps({"attempt_id": "fixture-2024-s0", "cost_usd": 0.25}) + "\n")
    return run


def test_export_keeps_identifiers_verdicts_and_cost_and_nothing_private(tmp_path):
    out = tmp_path / "out"
    export_run.export(make_run(tmp_path), out, "test", "unit test")

    config = json.loads((out / "run-config.json").read_text())
    assert config["label"] == "test"
    assert config["git_sha"] == "a" * 40
    assert "private-host" not in json.dumps(config)

    rows = list(csv.DictReader((out / "results.csv").read_text().splitlines()))
    assert tuple(rows[0]) == export_run.CSV_FIELDS
    first = next(r for r in rows if r["sample"] == "0" and r["problem"] == "fixture-2024-d1p1")
    assert (first["verdict"], first["outcome"], first["cost_usd"]) == ("pass", "pass_on_time", "0.25")
    assert "partial_score" in first
    published = {p.name for p in out.iterdir()}
    assert published == {"run-config.json", "results.csv", "summary.md", "index.html"}
    assert "private" not in "".join(p.read_text() for p in out.iterdir() if p.suffix != ".html")
    assert "mean partial_score" in (out / "summary.md").read_text()

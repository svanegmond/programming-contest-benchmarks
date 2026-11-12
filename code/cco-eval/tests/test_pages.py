from __future__ import annotations

import json
from pathlib import Path

from cco_eval.pages import public_run_config, publish, publish_site
from cco_eval.utils import write_json
from cco_eval.waterloo import Attempt, Mode, Outcome3, Verdict, to_json


def test_publish_writes_html_and_run_config_with_git_sha(tmp_path):
    run = tmp_path / "raw"
    attempt_dir = run / "p" / "s1"
    attempt_dir.mkdir(parents=True)
    attempt = Attempt("a1", "p", Mode.SINGLE_SHOT, "openrouter/m", "openrouter", "cpp",
                      "env", "hash", "t", None, 1)
    verdict = Verdict("a1", "p", {"all": True}, True, 0.75, Outcome3.PASS_ON_TIME, "env")
    write_json(attempt_dir / "attempt.json", to_json(attempt))
    write_json(attempt_dir / "verdict.json", to_json(verdict))
    write_json(run / "run.json", {
        "run_id": "r1", "git_sha": "a" * 40, "env_id": "env", "cohort_hash": "b" * 64,
        "models": ["openrouter/m"], "mode": "single_shot",
        "caps": {"max_tokens": {"openrouter/m": 65536}, "reasoning_effort": {"openrouter/m": "high"},
                 "backend": "vm"},
        "budget": None, "problem_ids": ["p"], "timeout_s": 7200,
        "judge": {"ssh_host": "user@private-host"},
        "k": 1, "temperature": 0.0, "reasoning_effort": "high", "lane": "cpp",
        "selection": "pass_at_k", "prompt_version": "cco-v2", "multi_submit": False,
    })
    site = tmp_path / "site"
    publish(run, site)
    config = json.loads((site / "run-config.json").read_text())
    html = (site / "index.html").read_text()
    summary = (site / "summary.md").read_text()
    assert config["git_sha"] == "a" * 40
    assert config["reasoning_effort"] == "high"
    assert config["k"] == 1
    assert config["lane"] == "cpp"
    assert "private-host" not in json.dumps(config)
    assert "mean partial_score" in html
    assert "correctness" in html
    assert "0.75" in html
    assert "Download run-config.json" in html
    assert "pass@k" in summary
    assert "withheld" not in html and "withheld" not in summary
    assert config["reasoning_published"] is False
    assert public_run_config(json.loads((run / "run.json").read_text()))["prompt_version"] == "cco-v2"


def test_overview_says_not_recorded_when_no_local_traces(tmp_path):
    run = tmp_path / "raw"
    attempt_dir = run / "p" / "s1"
    attempt_dir.mkdir(parents=True)
    attempt = Attempt("a1", "p", Mode.SINGLE_SHOT, "openrouter/m", "openrouter", "cpp",
                      "env", "hash", "t", None, 1)
    verdict = Verdict("a1", "p", {"all": True}, True, 0.75, Outcome3.PASS_ON_TIME, "env")
    write_json(attempt_dir / "attempt.json", to_json(attempt))
    write_json(attempt_dir / "verdict.json", to_json(verdict))
    write_json(run / "run.json", {
        "run_id": "raw", "git_sha": "a" * 40, "models": ["openrouter/m"], "mode": "single_shot",
        "k": 1, "caps": {},
    })
    site = tmp_path / "site"
    publish_site([run], site, notes=["n"])
    index = (site / "index.html").read_text()
    assert ">not recorded<" in index
    assert "withheld" not in index
    assert "Reasoning was not recorded for these runs." in index


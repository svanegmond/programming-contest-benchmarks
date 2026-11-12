from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from cco_eval.models import GenerateResult
from cco_eval.utils import write_json
from cco_eval.waterloo import Attempt, Mode, Outcome3, Verdict, to_json
from runners.common import ProblemData
from runners.judge_client import StubJudge
from waterloo import cli

MODEL = "openrouter/openai/gpt-oss-20b"
CATALOG = {
    "openai/gpt-oss-20b": {
        "id": "openai/gpt-oss-20b",
        "top_provider": {"max_completion_tokens": 32768},
        "supported_parameters": ["reasoning", "reasoning_effort", "temperature", "max_tokens"],
        "reasoning": {"supported_efforts": ["high", "medium", "low"], "default_effort": "medium"},
    }
}


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    (tmp_path / "a.in").write_text("2 3\n")
    (tmp_path / "a.out").write_text("5\n")
    tests = tmp_path / "tests.jsonl"
    tests.write_text(json.dumps({"id": "t1", "input_path": "a.in", "expected_path": "a.out"}) + "\n")
    problem = ProblemData("p", 2024, 1, "Sum", "Print the sum.", None, 1, 128, {}, tests, None)
    monkeypatch.setattr(cli, "load_problem", lambda pid: replace(problem, id=pid))
    monkeypatch.setattr(cli, "admitted_problem_ids", lambda root: {"p", "q", "r"})
    monkeypatch.setattr(cli, "RemoteJudge", lambda **kw: StubJudge())
    monkeypatch.setattr(cli, "load_openrouter_catalog", lambda **kw: CATALOG)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-sent")
    cohort = tmp_path / "cohort.json"
    write_json(cohort, {"seed": 1, "deterministic": ["p", "q"], "unusual": []})
    argv = ["run", "--models", MODEL, "--cohort", str(cohort), "--mode", "single_shot",
            "--lane", "python", "--out", str(tmp_path / "runs")]
    return tmp_path, argv


def test_help_is_available_from_checkout_without_pythonpath():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-m", "waterloo", "--help"], cwd=cli.REPO_ROOT,
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert "{admit,cohort,run,report,publish}" in result.stdout


def test_rerun_skips_finished_attempts(experiment, monkeypatch, capsys):
    root, argv = experiment
    calls = []
    response = GenerateResult("```python\nimport sys\nprint(sum(map(int, sys.stdin.read().split())))\n```",
                              "openrouter", MODEL, 5.0, 3, 4, "stop", {"usage": {"cost": 0.125}})
    monkeypatch.setattr("runners.single_shot.run_model", lambda *a, **kw: calls.append(a) or response)
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.count("skip finished") == 0
    assert len(calls) == 2
    run_dir = next((root / "runs").iterdir())
    manifest = json.loads((run_dir / "run.json").read_text())
    config = json.loads((run_dir / "run-config.json").read_text())
    assert len(manifest["git_sha"]) == 40
    assert len(manifest["cohort_hash"]) == 64
    assert manifest["models"] == [MODEL]
    assert manifest["budget"] is None
    assert manifest["k"] == 1
    assert manifest["lane"] == "python"
    assert manifest["reasoning_effort"] == "high"
    assert manifest["caps"]["max_tokens"][MODEL] == 65536
    assert manifest["caps"]["reasoning_effort"][MODEL] == "high"
    assert config["git_sha"] == manifest["git_sha"]
    assert cli.main(argv) == 0
    assert len(calls) == 2
    assert capsys.readouterr().out.count("skip finished") == 2
    telemetry = [json.loads(line) for line in (run_dir / "telemetry.jsonl").read_text().splitlines()]
    assert [row["cost_usd"] for row in telemetry] == [0.125, 0.125]
    assert len(list(run_dir.rglob("attempt.json"))) == 2
    assert cli.main(["report", run_dir.name, "--out", str(root / "runs")]) == 0
    out = capsys.readouterr().out
    assert "mean partial_score" in out
    assert "pass@k" in out


def test_k_writes_per_sample_artifacts(experiment, monkeypatch):
    root, argv = experiment
    response = GenerateResult("```python\nimport sys\nprint(sum(map(int, sys.stdin.read().split())))\n```",
                              "openrouter", MODEL, 1.0, 1, 1, "stop", {})
    monkeypatch.setattr("runners.single_shot.run_model", lambda *a, **kw: response)
    assert cli.main(argv + ["--k", "2", "--temperature", "0.2"]) == 0
    run_dir = next((root / "runs").iterdir())
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["k"] == 2
    assert manifest["temperature"] == 0.2
    assert len(list(run_dir.rglob("attempt.json"))) == 4
    samples = {path.parent.name for path in run_dir.rglob("attempt.json")}
    assert samples == {"s1", "s2"}


def test_dry_run_writes_config_without_an_api_key(experiment, monkeypatch, capsys):
    root, argv = experiment
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert cli.main(argv + ["--dry-run"]) == 0
    run_dir = next((root / "runs").iterdir())
    config = json.loads((run_dir / "run-config.json").read_text())
    assert config["reasoning_effort"] == "high"
    assert config["lane"] == "python"
    assert config["give_up_timeout_s"] == 120.0


def test_headline_lane_defaults_to_cpp(experiment, monkeypatch):
    root, argv = experiment
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    stripped = [item for item in argv if item not in {"--lane", "python"}]
    assert cli.main(stripped + ["--dry-run", "--run-id", "default-lane"]) == 0
    manifest = json.loads((root / "runs" / "default-lane" / "run.json").read_text())
    assert manifest["lane"] == "cpp"


def test_environment_change_is_not_relabelled_as_the_manifest_environment(experiment, monkeypatch, capsys):
    _, argv = experiment
    response = GenerateResult("```python\nimport sys\nprint(sum(map(int, sys.stdin.read().split())))\n```",
                              "openrouter", MODEL, 1.0, 1, 1, "stop", {})
    monkeypatch.setattr("runners.single_shot.run_model", lambda *a, **kw: response)
    grade = StubJudge.grade

    def changed(self, **kwargs):
        return {**grade(self, **kwargs), "env_id": "changed-environment"}

    monkeypatch.setattr(StubJudge, "grade", changed)
    assert cli.main(argv) == 1
    assert "judge environment changed" in capsys.readouterr().err
    assert cli.main(argv) == 1
    assert "invalid finished attempt" in capsys.readouterr().err


def test_explicit_run_id_refuses_different_caps(experiment, monkeypatch, capsys):
    _, argv = experiment
    response = GenerateResult("No program", "openrouter", MODEL, 1.0, 1, 1, "stop", {})
    monkeypatch.setattr("runners.single_shot.run_model", lambda *a, **kw: response)
    argv += ["--run-id", "named"]
    assert cli.main(argv) == 0
    assert cli.main(argv + ["--max-tokens", "100"]) == 1
    assert "run manifest differs" in capsys.readouterr().err


def test_agent_turn_budget_is_recorded_and_finished_attempt_is_resumable(experiment, monkeypatch, capsys):
    root, argv = experiment
    argv[argv.index("single_shot")] = "agentic"
    argv += ["--max-turns", "1"]
    monkeypatch.setattr("runners.agent_loop.run_model",
                        lambda *a, **k: GenerateResult("", "openrouter", MODEL, 1.0, 1, 1, "stop", {}))
    assert cli.main(argv) == 0
    run_dir = next((root / "runs").iterdir())
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["budget"]["max_turns"] == 1
    assert manifest["budget"]["wall_clock_s"] is None
    assert manifest["caps"]["max_tokens"][MODEL] == 65536
    assert len(list(run_dir.rglob("verdict.json"))) == 2
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.count("skip finished") == 2


def test_agent_harness_error_is_not_scored(experiment, monkeypatch, capsys):
    root, argv = experiment
    argv[argv.index("single_shot")] = "agentic"
    monkeypatch.setattr("runners.agent_loop.run_model", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    assert cli.main(argv) == 1
    assert "harness_error, not scored" in capsys.readouterr().err
    run_dir = next((root / "runs").iterdir())
    assert not list(run_dir.rglob("verdict.json"))


def test_telemetry_reports_agent_outcome_wall_time_and_last_finish_reason():
    call = {"input_tokens": 1, "output_tokens": 2, "latency_ms": 3.0, "stop_reason": "tool_calls",
            "raw_provider_metadata": {"provider": "Google", "usage": {"cost": 0.5}}}
    attempt = {"id": "a", "problem_id": "p", "model": MODEL, "language": "cpp", "turns_used": 2,
               "agent_outcome": "submitted", "wall_s": 9.5, "generation": {"calls": [call, call]}}
    row = cli.telemetry({"attempt": attempt})
    assert (row["cost_usd"], row["turns_used"], row["agent_outcome"], row["wall_s"]) == (1.0, 2, "submitted", 9.5)
    assert (row["finish_reason"], row["upstream_provider"]) == ("tool_calls", "Google")


@pytest.mark.parametrize("mode, extra, error", [
    ("single_shot", ["--budget", "1:1"], "wall-clock"),
    ("agentic", ["--budget", "0:1"], "wall-clock"),
    ("single_shot", ["--cluster-select"], "greater than 1"),
    ("single_shot", ["--multi-submit"], "only to agentic"),
])
def test_incompatible_budget_or_cap_is_rejected(experiment, mode, extra, error, capsys):
    _, argv = experiment
    argv[argv.index("single_shot")] = mode
    assert cli.main(argv + extra) == 1
    assert error in capsys.readouterr().err


def test_agentic_accepts_max_tokens(experiment, monkeypatch):
    _, argv = experiment
    argv[argv.index("single_shot")] = "agentic"
    monkeypatch.setattr("runners.agent_loop.run_model",
                        lambda *a, **k: GenerateResult("", "openrouter", MODEL, 1.0, 1, 1, "stop", {}))
    assert cli.main(argv + ["--max-tokens", "4000", "--max-turns", "1"]) == 0
    run_dir = next((experiment[0] / "runs").iterdir())
    assert json.loads((run_dir / "run.json").read_text())["caps"]["max_tokens"][MODEL] == 4000


def test_report_counts_truncation_separately_even_when_code_passes(tmp_path, capsys):
    for index, outcome in enumerate([Outcome3.PASS_ON_TIME, Outcome3.PASS_OVER_TIME, Outcome3.FAIL, Outcome3.PASS_ON_TIME]):
        folder = tmp_path / "r" / str(index)
        attempt = Attempt(str(index), f"p{index}", Mode.SINGLE_SHOT, MODEL, "openrouter", None,
                          "env", "hash", "transcript", None, 1,
                          failure_kind="truncated" if index == 3 else None)
        verdict = Verdict(str(index), f"p{index}", {}, outcome != Outcome3.FAIL, 0.5 if outcome != Outcome3.FAIL else 0,
                          outcome, "env")
        write_json(folder / "attempt.json", to_json(attempt))
        write_json(folder / "verdict.json", to_json(verdict))
    assert cli.main(["report", "r", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "mean partial_score" in out
    assert "| 1 | 1 | 1 | 1 |" in out or "1 |" in out


def test_cohort_draw_is_repeatable(tmp_path):
    path = tmp_path / "cohort.json"
    argv = ["cohort", "--out", str(path), "--deterministic", "2", "--unusual", "0"]
    assert cli.main(argv) == 0
    first = path.read_bytes()
    assert cli.main(argv) == 0
    assert path.read_bytes() == first
    assert len(json.loads(first)["deterministic"]) == 2


def test_admitted_problem_ids_skips_unadmitted_problems(tmp_path):
    for name, status in (("a", "admitted"), ("b", "rejected")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "problem.json").write_text("{}")
        (tmp_path / name / "status.json").write_text(json.dumps({"status": status}))
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "problem.json").write_text("{}")

    assert cli.admitted_problem_ids(tmp_path) == {"a"}

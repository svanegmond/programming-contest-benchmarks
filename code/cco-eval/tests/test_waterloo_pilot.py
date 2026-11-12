from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from cco_eval.models import GenerateResult
from cco_eval.run_records import load_run
from runners.common import ProblemData, resolve_max_tokens
from runners.judge_client import StubJudge
from runners.single_shot import run_one

FIXTURES = Path(__file__).parent / "fixtures/waterloo"
CODE = "```python\nimport sys\na, b = map(int, sys.stdin.read().split())\nprint(a+b)\n```"


def sum_problem(root: Path) -> ProblemData:
    (root / "a.in").write_text("2 3\n")
    (root / "a.out").write_text("5\n")
    tests = root / "tests.jsonl"
    tests.write_text(json.dumps({"id": "t1", "input_path": "a.in", "expected_path": "a.out"}) + "\n")
    return ProblemData("p", 2024, 1, "Sum", "Print the sum.", None, 1, 128, {}, tests, None)


def attempt(root: Path, text: str, run_id: str = "r", **kwargs):
    problem = sum_problem(root)
    result = GenerateResult(text, "openrouter", "m", 5.0, 3, 4, "stop", {"usage": {"cost": 0.1}})
    with patch("runners.single_shot.run_model", return_value=result):
        return run_one(problem, "mock/m", 1, out=root / "out", run_id=run_id, judge=StubJudge(),
                       timeout=1, max_tokens=10, **kwargs)


def test_graded_attempt_and_no_code_attempt_both_load(tmp_path):
    attempt(tmp_path, CODE, run_id="good")
    attempt(tmp_path, "I cannot solve this.", run_id="bad")
    good = load_run(tmp_path / "out/good")
    bad = load_run(tmp_path / "out/bad")
    assert good[0].verdict.outcome == "pass_on_time"
    assert (bad[0].verdict.all_pass, bad[0].verdict.outcome) == (False, "fail")
    assert bad[0].attempt.language is None
    assert bad[0].attempt.failure_kind == "no_program"


def test_checker_beside_problem_json_reaches_the_judge_with_the_attempt(tmp_path):
    problem_dir = tmp_path / "problem"
    problem_dir.mkdir()
    (problem_dir / "problem.json").write_text("{}")
    (problem_dir / "checker.py").write_text("def check(i, e, o):\n    return {'ok': True}\n")
    problem = sum_problem(tmp_path)
    problem = ProblemData(**{**problem.__dict__, "problem_json_path": problem_dir / "problem.json"})
    staged = {}

    class RecordingJudge(StubJudge):
        def grade(self, **kwargs):
            staged["names"] = sorted(p.name for p in kwargs["problem_json"].parent.iterdir())
            return super().grade(**kwargs)

    result = GenerateResult(CODE, "openrouter", "m", 5.0, 3, 4, "stop", {})
    with patch("runners.single_shot.run_model", return_value=result):
        run_one(problem, "mock/m", 1, out=tmp_path / "out", run_id="r", judge=RecordingJudge(), timeout=1)
    assert "checker.py" in staged["names"]


def test_single_shot_records_effective_max_tokens_and_truncation_kind(tmp_path):
    problem = sum_problem(tmp_path)
    response = GenerateResult(
        "internal reasoning only", "openrouter", "google/gemini-3.8-flash", 5.0,
        3, 100, "length", {"usage": {"completion_tokens_details": {"reasoning_tokens": 90}}},
    )
    with patch("runners.single_shot.run_model", return_value=response) as run_model:
        result = run_one(problem, "openrouter/google/gemini-3.8-flash", 1,
                         out=tmp_path / "out", run_id="truncated", judge=StubJudge(), timeout=1)

    assert run_model.call_args.args[-1] == 65536
    assert result["attempt"]["generation"]["max_tokens"] == 65536
    assert result["attempt"]["failure_kind"] == "truncated"
    assert result["verdict"]["failure_kind"] == "truncated"
    assert result["verdict"]["outcome"] == "fail"


def test_truncated_response_with_unclosed_program_is_still_judged_and_flagged(tmp_path):
    problem = sum_problem(tmp_path)
    unclosed = CODE.removesuffix("```")
    response = GenerateResult(unclosed, "openrouter", "m", 5.0, 3, 4, "length", {})
    with patch("runners.single_shot.run_model", return_value=response):
        result = run_one(problem, "mock/m", 1, out=tmp_path / "out", run_id="cut", judge=StubJudge(), timeout=1)

    assert result["attempt"]["failure_kind"] == "truncated"
    assert result["attempt"]["language"] == "python"
    assert result["verdict"]["outcome"] == "pass_on_time"


def test_openrouter_output_caps_match_model_catalog_and_allow_override():
    assert resolve_max_tokens("openrouter/google/gemini-3.8-flash") == 65536
    assert resolve_max_tokens("openrouter/openai/gpt-oss-20b") == 65536
    assert resolve_max_tokens("openrouter/openai/gpt-oss-20b", 12000) == 12000


def test_ungraded_attempt_keeps_stdout_and_has_no_verdict(tmp_path):
    result = attempt(tmp_path, CODE, grade=False)
    artifact = Path(result["artifact"])
    assert result["verdict"]["status"] == "pending_checker"
    assert not list(artifact.rglob("verdict.json"))
    rows = [json.loads(line) for line in (artifact / "run_results.jsonl").read_text().splitlines()]
    assert [row["verdict"] for row in rows] == ["ran"]
    assert Path(rows[0]["stdout_file"]).is_file()
    assert (artifact / "pending_checker.json").is_file()


def test_pending_attempts_are_skipped_by_the_run_loader(tmp_path):
    attempt(tmp_path, CODE, run_id="r")
    pending = tmp_path / "out/r/q"
    problem = sum_problem(tmp_path)
    problem.id = "q"
    result = GenerateResult(CODE, "openrouter", "m", 5.0, 3, 4, "stop", {})
    with patch("runners.single_shot.run_model", return_value=result):
        run_one(problem, "mock/m", 1, out=tmp_path / "out", run_id="r", judge=StubJudge(),
                timeout=1, max_tokens=10, grade=False)
    assert pending.is_dir() and len(load_run(tmp_path / "out/r")) == 1


def test_remote_judge_uploads_the_checker_with_the_attempt(tmp_path, monkeypatch):
    from runners import judge_client

    sent = []
    monkeypatch.setattr(judge_client, "_run", lambda command, **kw: sent.append(command))
    judge = judge_client.RemoteJudge(ssh_host="host")
    monkeypatch.setattr(judge, "_ssh", lambda script, timeout=None: '{"all_pass": true}')
    for name in ("sol.py", "problem.json", "tests.jsonl", "checker.py"):
        (tmp_path / name).write_text("x")

    judge.grade(attempt_id="a1", code_path=tmp_path / "sol.py", lang="python",
                problem_json=tmp_path / "problem.json", tests_jsonl=tmp_path / "tests.jsonl",
                out_dir=tmp_path / "out")

    uploaded = {Path(c[1]).name for c in sent if c[0] == "scp" and Path(c[1]) == tmp_path / Path(c[1]).name}
    assert uploaded == {"sol.py", "problem.json", "tests.jsonl", "checker.py"}

    sent.clear()
    (tmp_path / "checker.py").unlink()
    judge.grade(attempt_id="a2", code_path=tmp_path / "sol.py", lang="python",
                problem_json=tmp_path / "problem.json", tests_jsonl=tmp_path / "tests.jsonl",
                out_dir=tmp_path / "out")
    assert "checker.py" not in {Path(c[1]).name for c in sent if c[0] == "scp"}


def test_remote_judge_run_returns_the_head_of_the_program_output(tmp_path, monkeypatch):
    from runners import judge_client

    def fake_run(command, **kw):
        if command[0] == "scp" and command[1].startswith("host:"):
            Path(command[2]).write_text("15\n" + "x" * 5000)

    monkeypatch.setattr(judge_client, "_run", fake_run)
    judge = judge_client.RemoteJudge(ssh_host="host")
    monkeypatch.setattr(judge, "_ssh", lambda script, timeout=None: '{"verdict": "ran", "stdout_path": "/remote/out"}')
    (tmp_path / "sol.py").write_text("x")
    (tmp_path / "in.txt").write_text("x")
    outputs = tmp_path / "outputs"

    result = judge.run("python", tmp_path / "sol.py", tmp_path / "in.txt", 1.0, attempt_id="t1", output_dir=outputs)

    assert result["stdout_head"].startswith("15\n") and len(result["stdout_head"]) == 2000
    assert [p.name for p in outputs.iterdir()] == ["t1-stdout.txt"]
    assert not (tmp_path / "t1-stdout.txt").exists()

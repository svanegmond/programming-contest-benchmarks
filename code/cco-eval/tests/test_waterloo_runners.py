from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cco_eval.providers import generate_openrouter
from cco_eval.waterloo import AgentOutcome, Lane
from runners.agent_loop import _coarse_verdict, run_agent
from runners.common import ProblemData, build_single_prompt, build_single_system_prompt, load_problem
from runners.judge_client import StubJudge
from runners.single_shot import extract_solution


class ScriptedModel:
    def __init__(self, responses: list[Any] | None = None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error

    def complete(self, messages, tools, *, timeout):
        if self.error:
            raise self.error
        if not self.responses:
            return {"role": "assistant", "content": ""}
        return self.responses.pop(0)


def tool(name: str, **arguments: object) -> dict[str, Any]:
    return {"role": "assistant", "tool_calls": [{"id": f"call-{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def setup_problem(root: Path) -> ProblemData:
    problem_dir = root / "problem"
    problem_dir.mkdir()
    (problem_dir / "sample.in").write_text("2 3\n", encoding="utf-8")
    (problem_dir / "sample.out").write_text("5\n", encoding="utf-8")
    (problem_dir / "hidden.in").write_text("9 2\n", encoding="utf-8")
    (problem_dir / "hidden.out").write_text("11\n", encoding="utf-8")
    tests = problem_dir / "tests.jsonl"
    tests.write_text("\n".join([
        json.dumps({"id": "sample-1", "kind": "sample", "input_path": "sample.in", "expected_path": "sample.out"}),
        json.dumps({"id": "hidden-1", "kind": "official", "input_path": "hidden.in", "expected_path": "hidden.out"}),
    ]) + "\n", encoding="utf-8")
    problem_json = problem_dir / "problem.json"
    problem_json.write_text(json.dumps({"id": "fixture", "year": 2024, "day": 1,
        "title": "Add two integers", "statement": "Read two integers and print their sum.",
        "time_limit_s": 1, "memory_limit_mb": 128}), encoding="utf-8")
    return ProblemData("fixture", 2024, 1, "Add two integers",
        "Read two integers and print their sum.", None, 1, 128, {}, tests, problem_json,
        samples=[("2 3\n", "5\n")])


def run_script(tmp_path: Path, problem: ProblemData, client: Any, *, run: str,
               max_turns: int = 2, wall: float = 10):
    return run_agent(problem, "mock/script", 1, out=tmp_path / "out", run_id=run,
                     judge=StubJudge(), wall_clock_s=wall, max_turns=max_turns,
                     request_timeout_s=1, model_client=client)


def test_every_agent_outcome_and_budget_auto_submit(tmp_path):
    problem = setup_problem(tmp_path)
    code = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a+b)\n"
    submitted = run_script(tmp_path, problem, ScriptedModel([
        tool("write_file", path="solution.py", content=code),
        tool("submit", lang="python", code_path="solution.py"),
    ]), run="submitted")
    assert submitted["attempt"]["agent_outcome"] == AgentOutcome.SUBMITTED
    assert not submitted["attempt"]["auto_submitted"]
    assert submitted["verdict"]["all_pass"]

    budget = run_script(tmp_path, problem,
        ScriptedModel([tool("write_file", path="solution.py", content=code)]),
        run="turn-budget", max_turns=1)
    assert budget["attempt"]["agent_outcome"] == AgentOutcome.TURN_BUDGET_EXHAUSTED
    assert budget["attempt"]["auto_submitted"]
    assert budget["verdict"]["all_pass"]

    def slow_client(messages, tools, timeout):
        if not any(message.get("role") == "assistant" for message in messages):
            return tool("write_file", path="solution.py", content=code)
        time.sleep(0.05)
        return {"role": "assistant", "content": ""}

    slow = run_script(tmp_path, problem, slow_client, run="no-wall", max_turns=2)
    assert slow["attempt"]["agent_outcome"] == AgentOutcome.TURN_BUDGET_EXHAUSTED
    assert slow["attempt"]["auto_submitted"]
    assert slow["attempt"]["turns_used"] == 2

    empty = run_script(tmp_path, problem,
        ScriptedModel([{"role": "assistant", "content": ""}]), run="empty")
    assert empty["attempt"]["agent_outcome"] == AgentOutcome.NO_SOLUTION_WRITTEN
    assert not empty["attempt"]["auto_submitted"]

    broken = run_script(tmp_path, problem,
        ScriptedModel(error=RuntimeError("scripted failure")), run="error")
    assert broken["attempt"]["agent_outcome"] == AgentOutcome.HARNESS_ERROR


def test_agent_workspace_contains_only_statement_and_samples(tmp_path):
    problem = setup_problem(tmp_path)
    result = run_script(tmp_path, problem,
        ScriptedModel([{"role": "assistant", "content": ""}]), run="workspace")
    workspace = Path(result["artifact"]) / "workspace"
    files = {path.name for path in workspace.iterdir() if path.is_file()}
    assert files == {"statement.md", "sample-0.in", "sample-0.out"}
    assert "hidden.in" not in files
    assert "tests.jsonl" not in files
    prompt = (Path(result["artifact"]) / "prompt.txt").read_text()
    assert "published past Canadian Computing Olympiad" in prompt
    assert "brute-force" in prompt
    assert "run_custom" in prompt
    assert "300" not in prompt


def test_run_tool_shows_the_model_its_program_output_and_keeps_run_files_out_of_the_workspace(tmp_path):
    problem = setup_problem(tmp_path)
    code = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a+b)\n"
    result = run_script(tmp_path, problem, ScriptedModel([
        tool("write_file", path="solution.py", content=code),
        tool("run", lang="python", code_path="solution.py", stdin_path="sample-0.in"),
        {"role": "assistant", "content": ""},
    ]), run="run-tool", max_turns=3)
    artifact = Path(result["artifact"])
    calls = [json.loads(line) for line in (artifact / "transcript.jsonl").read_text().splitlines()]
    ran = next(c for c in calls if c.get("type") == "tool_call" and c["name"] == "run")
    assert ran["result"]["stdout_head"] == "5\n"
    assert ran["result"]["sample_pass"] is True
    assert ran["result"]["diff"] == ""
    assert {p.name for p in (artifact / "workspace").iterdir()} == {
        "statement.md", "sample-0.in", "sample-0.out", "solution.py"}
    assert list((artifact / "run-output").iterdir())


def test_rerun_of_an_attempt_starts_from_a_clean_workspace(tmp_path):
    problem = setup_problem(tmp_path)
    run_script(tmp_path, problem, ScriptedModel([
        tool("write_file", path="old.py", content="print(1)\n")]), run="again", max_turns=1)
    result = run_script(tmp_path, problem,
        ScriptedModel([{"role": "assistant", "content": ""}]), run="again")
    files = {p.name for p in (Path(result["artifact"]) / "workspace").iterdir()}
    assert files == {"statement.md", "sample-0.in", "sample-0.out"}


def test_single_shot_extracts_language_from_fence():
    lang, code = extract_solution("Here it is:\n```cpp\nint main() {}\n```\n")
    assert lang.value == "cpp"
    assert code == "int main() {}\n"
    lang, code = extract_solution("```python\nprint(3)\n```")
    assert lang.value == "python"


def test_single_shot_recovers_bare_language_label_from_saved_truncated_response():
    fixture = Path(__file__).parent / "fixtures/waterloo/gemini-2025-truncated-response.txt"
    response = fixture.read_text(encoding="utf-8")
    assert response.startswith("cpp\n#include")
    assert "```" not in response

    lang, code = extract_solution(response)

    assert lang.value == "cpp"
    assert code.startswith("#include <iostream>")
    assert "using namespace std;" in code


def test_single_shot_recovers_unclosed_fence_from_saved_truncated_response():
    fixture = Path(__file__).parent / "fixtures/waterloo/gemini-2024-unclosed-fence-response.txt"
    response = fixture.read_text(encoding="utf-8")
    assert response.count("```") == 1

    lang, code = extract_solution(response)

    assert lang.value == "cpp"
    assert code.startswith("#include <iostream>")


def test_openrouter_result_preserves_cost_and_tool_calls():
    body = {"id": "gen-1", "model": "openai/test", "provider": "test-provider",
            "choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "submit", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "cost": 0.0001}}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(body).encode()

    with patch("urllib.request.urlopen", return_value=Response()) as urlopen:
        result = generate_openrouter("openai/test", [{"role": "user", "content": "hi"}],
            "key", 1, tools=[{"type": "function"}], reasoning_effort="low")
    assert result.provider == "openrouter"
    assert result.input_tokens == 4
    assert result.output_tokens == 2
    assert result.raw_provider_metadata["usage"]["cost"] == 0.0001
    assert result.raw_provider_metadata["tool_calls"][0]["id"] == "c1"
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/chat/completions")
    assert json.loads(request.data)["reasoning"] == {"effort": "low"}


def test_run_tool_reports_a_sample_mismatch_diff(tmp_path):
    problem = setup_problem(tmp_path)
    result = run_script(tmp_path, problem, ScriptedModel([
        tool("write_file", path="solution.py", content="print(0)\n"),
        tool("run", lang="python", code_path="solution.py", stdin_path="sample-0.in"),
        {"role": "assistant", "content": ""},
    ]), run="mismatch", max_turns=3)
    calls = [json.loads(line) for line in (Path(result["artifact"]) / "transcript.jsonl").read_text().splitlines()]
    ran = next(c for c in calls if c.get("type") == "tool_call" and c["name"] == "run")
    assert ran["result"]["sample_pass"] is False
    assert "5" in ran["result"]["diff"] and "0" in ran["result"]["diff"]


def test_run_custom_accepts_model_written_stdin(tmp_path):
    problem = setup_problem(tmp_path)
    code = "import sys\nprint(sys.stdin.read().strip())\n"
    result = run_script(tmp_path, problem, ScriptedModel([
        tool("write_file", path="solution.py", content=code),
        tool("run_custom", lang="python", code_path="solution.py", stdin="hello-custom\n"),
        {"role": "assistant", "content": ""},
    ]), run="custom", max_turns=3)
    calls = [json.loads(line) for line in (Path(result["artifact"]) / "transcript.jsonl").read_text().splitlines()]
    ran = next(c for c in calls if c.get("type") == "tool_call" and c["name"] == "run_custom")
    assert "hello-custom" in ran["result"]["stdout_head"]
    assert "official" in ran["result"]["note"]


def test_multi_submit_returns_verdict_only_and_counts_failures(tmp_path):
    problem = setup_problem(tmp_path)
    wrong = "print(0)\n"
    right = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a+b)\n"
    result = run_agent(
        problem, "mock/script", 1, out=tmp_path / "out", run_id="multi",
        judge=StubJudge(), max_turns=4, request_timeout_s=1, multi_submit=True,
        model_client=ScriptedModel([
            tool("write_file", path="wrong.py", content=wrong),
            tool("submit", lang="python", code_path="wrong.py"),
            tool("write_file", path="solution.py", content=right),
            tool("submit", lang="python", code_path="solution.py"),
        ]),
    )
    assert result["attempt"]["agent_outcome"] == AgentOutcome.SUBMITTED
    assert result["attempt"]["failed_submits"] == 1
    assert result["verdict"]["all_pass"]
    transcript = (Path(result["artifact"]) / "transcript.jsonl").read_text()
    assert "wrong_answer" in transcript
    assert "partial_score" not in transcript
    assert "hidden-1" not in transcript


def test_real_problem_prompt_and_workspace_hold_only_that_problem(tmp_path):
    problem = load_problem("cco-2016-d1p1")
    prompt = build_single_prompt(problem)
    system = build_single_system_prompt(problem, Lane.CPP)
    assert "Field Trip" in prompt
    assert "8 5 2" in prompt
    assert "published past Canadian Computing Olympiad" in system
    assert "short rationale" in system
    assert "bits/stdc++.h" in system
    assert "Respect the time limit" not in system
    assert "tight olympiad" not in build_single_system_prompt(problem, Lane.FREE)
    assert "tight limits" not in system
    for other in ("Splitting Hares", "Legends", "O Canada", "Zombie Apocalypse", "Pirates"):
        assert other not in prompt
        assert other not in system
    assert len(prompt) < 8000
    result = run_script(tmp_path, problem, ScriptedModel([{"role": "assistant", "content": ""}]), run="real")
    workspace = Path(result["artifact"]) / "workspace"
    assert {path.name for path in workspace.iterdir()} == {"statement.md", "sample-0.in", "sample-0.out"}
    assert (workspace / "sample-0.in").read_text().startswith("8 5 2\n")
    assert (workspace / "sample-0.out").read_text() == "6 2\n"
    for other in ("Splitting Hares", "Legends"):
        assert other not in (workspace / "statement.md").read_text()


def test_coarse_verdict_accepts_correct_output_past_the_statement_limit(tmp_path):
    assert _coarse_verdict({"all_pass": True, "outcome": "pass_over_time"}, tmp_path) == "accepted"
    (tmp_path / "test_results.jsonl").write_text(json.dumps({"verdict": "killed"}) + "\n", encoding="utf-8")
    assert _coarse_verdict({"all_pass": False, "outcome": "fail"}, tmp_path) == "too_slow"


def test_stub_grade_slow_correct_output_is_all_pass(tmp_path):
    problem_dir = tmp_path / "problem"
    problem_dir.mkdir()
    (problem_dir / "in.txt").write_text("2 3\n", encoding="utf-8")
    (problem_dir / "out.txt").write_text("5\n", encoding="utf-8")
    tests = problem_dir / "tests.jsonl"
    tests.write_text(json.dumps({
        "id": "t1", "input_path": "in.txt", "expected_path": "out.txt", "subtask": "all",
    }) + "\n", encoding="utf-8")
    (problem_dir / "problem.json").write_text(json.dumps({
        "id": "slow", "time_limit_s": 0.05, "memory_limit_mb": 128,
    }), encoding="utf-8")
    code = problem_dir / "sol.py"
    code.write_text(
        "import time, sys\ntime.sleep(0.12)\nprint(sum(map(int, sys.stdin.read().split())))\n",
        encoding="utf-8",
    )
    verdict = StubJudge().grade(
        attempt_id="a", code_path=code, lang="python",
        problem_json=problem_dir / "problem.json", tests_jsonl=tests, out_dir=tmp_path / "grade",
    )
    assert verdict["all_pass"] is True
    assert verdict["outcome"] == "pass_over_time"


def test_model_tool_mistakes_go_back_to_the_model_not_harness_error(tmp_path):
    problem = setup_problem(tmp_path)
    code = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a+b)\n"
    result = run_script(tmp_path, problem, ScriptedModel([
        tool("read_file", path="missing.txt"),
        tool("read_file", path="../../etc/passwd"),
        tool("write_file", path="solution.py", content=code),
        tool("submit", lang="python", code_path="solution.py"),
    ]), run="tool-mistakes", max_turns=5)
    assert result["attempt"]["agent_outcome"] == AgentOutcome.SUBMITTED
    assert result["verdict"]["all_pass"]
    transcript = (Path(result["artifact"]) / "transcript.jsonl").read_text()
    assert "no such file" in transcript and "must stay inside" in transcript

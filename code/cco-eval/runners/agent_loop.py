"""Small tool-using Waterloo agent, deliberately independent of agent frameworks."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cco_eval.tracing import JsonlTracer
from cco_eval.utils import sha256_text
from cco_eval.waterloo import AgentOutcome, Attempt, Budget, Language, Mode
from runners.common import (
    DEFAULT_CORPUS, DEFAULT_JUDGE_PATH, DEFAULT_ZIPS_ROOT, ProblemData,
    append_result, attempt_dir, create_run_id, dump_attempt, load_problem,
    record_statement_file, resolve_problems, run_env_id, run_model,
)
from runners.judge_client import DEFAULT_SSH_HOST, RemoteJudge, StubJudge
from runners.single_shot import _prepared_grade_files

TOOL_SPECS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a file in the attempt workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write a file in the attempt workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "List files in the attempt workspace.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "run", "description": "Compile and run code on a sample input only.",
     "parameters": {"type": "object", "properties": {"lang": {"type": "string", "enum": ["cpp", "python"]},
      "code_path": {"type": "string"}, "stdin_path": {"type": "string"}}, "required": ["lang", "code_path", "stdin_path"]}}},
    {"type": "function", "function": {"name": "submit", "description": "Submit one solution without score feedback.",
     "parameters": {"type": "object", "properties": {"lang": {"type": "string", "enum": ["cpp", "python"]},
      "code_path": {"type": "string"}}, "required": ["lang", "code_path"]}}},
]


class ToolInputError(ValueError):
    """A tool call the model got wrong; reported back to the model, not a harness failure."""


def _inside(workspace: Path, value: str) -> Path:
    root = workspace.resolve()
    path = (workspace / value).resolve()
    if not path.is_relative_to(root):
        raise ToolInputError("tool path must stay inside the attempt workspace")
    return path


def _agent_prompt(problem: ProblemData, samples: list[tuple[Path, Path]], budget: Budget) -> str:
    samples_text = "\n".join(f"- {a.name} (expected output {b.name})" for a, b in samples) or "- no samples available"
    return f"""Solve the programming problem in statement.md. Allowed languages: C++ gnu++20 with g++, or Python 3 run with PyPy3. Use standard library only, read stdin and write stdout. Time limit: {problem.time_limit_s:g} seconds; memory limit: {problem.memory_limit_mb or 'not specified'} MB. The attempt budget is {budget.wall_clock_s:g} seconds and {budget.max_turns} model turns.

Files available:
- statement.md
{samples_text}

Use read_file/write_file/list_files and run only on sample inputs. Hidden tests are not available. You may submit exactly once with submit(lang, code_path). Submission is blind: you will receive only confirmation, never grading or hidden-test feedback. Save a complete solution file before submitting.
"""


def _assistant_payload(response: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(response, dict):
        message = response.get("message", response)
        if not isinstance(message, dict):
            raise TypeError("mock model response message must be an object")
        return dict(message), dict(response.get("generation", {}))
    metadata = response.raw_provider_metadata or {}
    message = {"role": "assistant", "content": response.text}
    if metadata.get("tool_calls"):
        message["tool_calls"] = metadata["tool_calls"]
    return message, response.to_record()


def _call_model(client: Any, messages: list[dict[str, Any]], timeout: float) -> Any:
    if hasattr(client, "complete"):
        return client.complete(messages, TOOL_SPECS, timeout=timeout)
    return client(messages, TOOL_SPECS, timeout)


def run_agent(
    problem: ProblemData, model: str, sample_index: int, *, out: Path, run_id: str,
    judge: Any, wall_clock_s: float = 300, max_turns: int = 20,
    request_timeout_s: float = 180, model_client: Any = None,
) -> dict[str, Any]:
    started = time.monotonic()
    budget = Budget(wall_clock_s=wall_clock_s, max_turns=max_turns)
    artifact = attempt_dir(out, run_id, problem.id, Mode.AGENTIC.value, model, sample_index)
    workspace = artifact / "workspace"
    shutil.rmtree(artifact, ignore_errors=True)
    artifact.mkdir(parents=True)
    samples = _copy_workspace(problem, workspace)
    prompt = _agent_prompt(problem, samples, budget)
    prompt_hash = sha256_text(prompt)
    (artifact / "prompt.txt").write_text(prompt, encoding="utf-8")
    record_statement_file(problem, artifact)
    trace = JsonlTracer(artifact / "trace.jsonl")
    transcript_path = artifact / "transcript.jsonl"
    messages: list[dict[str, Any]] = [{"role": "system", "content": "You are a careful contest programmer. Follow the tools and submission rules."},
                                     {"role": "user", "content": prompt}]
    with transcript_path.open("w", encoding="utf-8") as transcript:
        for message in messages:
            _write_transcript(transcript, {"type": "message", **message})
        client = model_client or _OpenRouterClient(model)
        latest_solution: tuple[str, Path] | None = None
        submitted: tuple[str, Path] | None = None
        outcome: AgentOutcome | None = None
        turns_used = 0
        generations: list[dict[str, Any]] = []
        error: str | None = None
        for turn in range(1, max_turns + 1):
            remaining = wall_clock_s - (time.monotonic() - started)
            if remaining <= 0:
                outcome = AgentOutcome.WALL_CLOCK_EXHAUSTED
                break
            turns_used = turn
            try:
                with trace.span("agent.turn", {"turn": turn, "model": model}):
                    response = _call_model(client, messages, min(request_timeout_s, remaining))
                assistant, generation = _assistant_payload(response)
                assistant.setdefault("role", "assistant")
                generations.append(generation)
                messages.append(assistant)
                _write_transcript(transcript, {"type": "message", **assistant, "turn": turn,
                                               "generation": generation})
                calls = assistant.get("tool_calls", [])
                if not calls:
                    if not assistant.get("content") and not latest_solution:
                        outcome = AgentOutcome.NO_SOLUTION_WRITTEN
                        break
                    messages.append({"role": "user", "content": "Use the tools to save your solution and call submit(lang, code_path)."})
                    _write_transcript(transcript, {"type": "message", "role": "user",
                                                   "content": "Use the tools to save your solution and call submit(lang, code_path)."})
                    continue
                for call in calls:
                    if time.monotonic() - started >= wall_clock_s:
                        outcome = AgentOutcome.WALL_CLOCK_EXHAUSTED
                        break
                    call_id = str(call.get("id", uuid.uuid4().hex))
                    function = call.get("function", {})
                    name = str(function.get("name", ""))
                    args = function.get("arguments", {})
                    call_started = time.monotonic()
                    with trace.span("agent.tool", {"tool": name, "turn": turn, "call_id": call_id}) as span:
                        try:
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError as exc:
                                    raise ToolInputError(f"arguments are not valid JSON: {exc}") from exc
                            if not isinstance(args, dict):
                                raise ToolInputError(f"{name} arguments must be an object")
                            try:
                                result = _execute_tool(name, args, workspace, judge, problem, run_id, turn,
                                                      artifact / "run-output")
                            except KeyError as exc:
                                raise ToolInputError(f"missing argument {exc}") from exc
                        except ToolInputError as input_exc:
                            result = {"error": str(input_exc)}
                        except Exception as tool_exc:
                            elapsed_ms = round((time.monotonic() - call_started) * 1000, 3)
                            _write_transcript(transcript, {"type": "tool_call", "id": call_id,
                                "name": name, "arguments": args,
                                "error": f"{type(tool_exc).__name__}: {tool_exc}",
                                "duration_ms": elapsed_ms, "turn": turn})
                            span.set_error(str(tool_exc), tool_exc)
                            raise
                        span.set_attribute("result_chars", len(json.dumps(result, ensure_ascii=False)))
                    elapsed_ms = round((time.monotonic() - call_started) * 1000, 3)
                    call_record = {"type": "tool_call", "id": call_id, "name": name,
                                   "arguments": args, "result": result, "duration_ms": elapsed_ms,
                                   "turn": turn}
                    _write_transcript(transcript, call_record)
                    tool_message = {"role": "tool", "tool_call_id": call_id,
                                    "name": name, "content": json.dumps(result, ensure_ascii=False)}
                    messages.append(tool_message)
                    if name == "write_file" and result.get("ok"):
                        lang = _language_from_path(str(result["path"]))
                        if lang:
                            latest_solution = (lang, _inside(workspace, str(result["path"])))
                    if name == "submit" and result.get("accepted"):
                        submitted = (str(args["lang"]), _inside(workspace, str(args["code_path"])))
                        outcome = AgentOutcome.SUBMITTED
                        break
                    if time.monotonic() - started >= wall_clock_s:
                        outcome = AgentOutcome.WALL_CLOCK_EXHAUSTED
                        break
                if outcome is not None:
                    break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                outcome = AgentOutcome.WALL_CLOCK_EXHAUSTED if time.monotonic() - started >= wall_clock_s else AgentOutcome.HARNESS_ERROR
                _write_transcript(transcript, {"type": "harness_error", "turn": turn, "error": error})
                break
        if outcome is None:
            outcome = AgentOutcome.TURN_BUDGET_EXHAUSTED

    auto_submitted = outcome in {AgentOutcome.WALL_CLOCK_EXHAUSTED, AgentOutcome.TURN_BUDGET_EXHAUSTED} and latest_solution is not None
    code_selection = submitted or (latest_solution if auto_submitted else None)
    if code_selection is None and outcome in {AgentOutcome.WALL_CLOCK_EXHAUSTED, AgentOutcome.TURN_BUDGET_EXHAUSTED}:
        outcome = AgentOutcome.NO_SOLUTION_WRITTEN
    if outcome == AgentOutcome.HARNESS_ERROR and latest_solution is not None:
        code_selection = latest_solution
    lang = Language(code_selection[0]) if code_selection else None
    code_path = code_selection[1] if code_selection else None
    attempt_id = f"{run_id}-{problem.id}-agent-{sample_index}-{uuid.uuid4().hex[:8]}"
    wall_s = time.monotonic() - started
    attempt = Attempt(
        id=attempt_id, problem_id=problem.id, mode=Mode.AGENTIC, model=model,
        provider="openrouter", language=lang, env_id=run_env_id(judge), prompt_hash=prompt_hash,
        transcript_path=str(transcript_path), code_path=str(code_path) if code_path else None,
        sample_index=sample_index, budget=budget, agent_outcome=outcome,
        auto_submitted=auto_submitted, turns_used=turns_used, wall_s=wall_s,
        generation={"calls": generations, "harness_error": error} if error else {"calls": generations},
    )
    dump_attempt(artifact / "attempt.json", attempt)
    verdict: dict[str, Any] = {"attempt_id": attempt_id, "problem_id": problem.id,
                              "agent_outcome": outcome.value, "auto_submitted": auto_submitted,
                              "all_pass": False, "outcome": "fail", "env_id": attempt.env_id}
    if code_path is not None and lang is not None and problem.tests_path is not None:
        problem_json, tests_jsonl = _prepared_grade_files(problem, artifact)
        verdict = judge.grade(attempt_id=attempt_id, code_path=code_path, lang=lang.value,
                              problem_json=problem_json, tests_jsonl=tests_jsonl,
                              out_dir=artifact / "judge")
        verdict["agent_outcome"] = outcome.value
        verdict["auto_submitted"] = auto_submitted
    append_result(out / run_id / "results.jsonl", attempt, verdict)
    return {"attempt": asdict(attempt), "verdict": verdict, "artifact": str(artifact)}


def _copy_workspace(problem: ProblemData, workspace: Path) -> list[tuple[Path, Path]]:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "statement.md").write_text(problem.statement, encoding="utf-8")
    samples: list[tuple[Path, Path]] = []
    for index, (sample_in, sample_out) in enumerate(problem.samples):
        local_in, local_out = workspace / f"sample-{index}.in", workspace / f"sample-{index}.out"
        local_in.write_text(sample_in.rstrip("\n") + "\n", encoding="utf-8")
        local_out.write_text(sample_out.rstrip("\n") + "\n", encoding="utf-8")
        samples.append((local_in, local_out))
    return samples


def _language_from_path(path: str) -> str | None:
    if Path(path).suffix.lower() in {".cpp", ".cc", ".cxx"}: return "cpp"
    if Path(path).suffix.lower() == ".py": return "python"
    return None


def _execute_tool(name: str, args: dict[str, Any], workspace: Path, judge: Any,
                  problem: ProblemData, run_id: str, turn: int, output_dir: Path) -> dict[str, Any]:
    if name == "list_files":
        return {"files": sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file())}
    if name == "read_file":
        path = _inside(workspace, str(args["path"]))
        if not path.is_file():
            raise ToolInputError(f"no such file: {args['path']}")
        return {"path": str(path.relative_to(workspace.resolve())), "content": path.read_text(encoding="utf-8", errors="replace")}
    if name == "write_file":
        path = _inside(workspace, str(args["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        return {"ok": True, "path": str(path.relative_to(workspace.resolve()))}
    if name == "run":
        lang, code_path = str(args["lang"]), _inside(workspace, str(args["code_path"]))
        stdin_path = _inside(workspace, str(args["stdin_path"]))
        if lang not in {"cpp", "python"} or not code_path.is_file() or not stdin_path.is_file():
            raise ToolInputError("run requires existing workspace code and sample input files")
        if not stdin_path.name.startswith("sample-"):
            raise ToolInputError("run is restricted to sample inputs")
        result = judge.run(lang, code_path, stdin_path, problem.time_limit_s,
                           attempt_id=f"{run_id}-turn{turn}", memory_mb=problem.memory_limit_mb,
                           output_dir=output_dir)
        return {key: result.get(key) for key in ("compile", "verdict", "cpu_s", "wall_s", "peak_memory_mb",
                                                 "stdout_head", "stderr_tail", "over_time", "over_memory")}
    if name == "submit":
        lang, code_path = str(args["lang"]), _inside(workspace, str(args["code_path"]))
        if lang not in {"cpp", "python"} or not code_path.is_file():
            raise ToolInputError("submit requires an existing solution file and a supported language")
        if _language_from_path(str(code_path)) != lang:
            raise ToolInputError("solution file extension does not match lang")
        return {"accepted": True, "message": "Submission received. No score or feedback will be provided."}
    raise ToolInputError(f"unknown tool: {name}")


def _write_transcript(stream: Any, record: dict[str, Any]) -> None:
    stream.write(json.dumps({"time": time.time(), **record}, sort_keys=True, ensure_ascii=False) + "\n")
    stream.flush()


class _OpenRouterClient:
    def __init__(self, model: str) -> None: self.model = model
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, timeout: float) -> Any:
        return run_model(self.model, messages, tools, timeout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--problems", required=True, help="Comma-separated IDs or 'admitted'.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--wall-clock-s", type=float, default=300)
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--judge", choices=("remote", "stub"), default="remote")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--judge-path", default=DEFAULT_JUDGE_PATH)
    parser.add_argument("--zips-root", default=DEFAULT_ZIPS_ROOT)
    parser.add_argument("--backend", choices=("vm", "host"), default="vm")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.k < 1 or args.max_turns < 1 or args.wall_clock_s <= 0:
        raise SystemExit("--k, --max-turns, and --wall-clock-s must be positive")
    problems = resolve_problems(args.problems, args.corpus)
    judge = StubJudge() if args.judge == "stub" else RemoteJudge(
        ssh_host=args.ssh_host, judge_path=args.judge_path, zips_root=args.zips_root,
        backend=args.backend)
    run_id = create_run_id()
    for problem_id in problems:
        problem = load_problem(problem_id, args.corpus)
        for sample_index in range(1, args.k + 1):
            result = run_agent(problem, args.model, sample_index, out=args.out, run_id=run_id,
                               judge=judge, wall_clock_s=args.wall_clock_s, max_turns=args.max_turns,
                               request_timeout_s=args.timeout)
            print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

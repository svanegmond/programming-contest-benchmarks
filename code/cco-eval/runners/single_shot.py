"""Single-generation Waterloo coding benchmark runner."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import urllib.error
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cco_eval.waterloo import Attempt, FailureKind, Language, Mode, Outcome3, Verdict, to_json
from cco_eval.utils import write_json
from runners.common import (
    DEFAULT_CORPUS, DEFAULT_JUDGE_PATH, DEFAULT_ZIPS_ROOT, ProblemData,
    append_result, attempt_dir, build_single_prompt, create_run_id, dump_attempt,
    load_problem, record_statement_file, resolve_max_tokens, resolve_problems, run_env_id,
    run_model, sha256_text,
)
from runners.judge_client import DEFAULT_SSH_HOST, RemoteJudge, StubJudge


def extract_solution(response: str) -> tuple[Language, str]:
    match = re.search(r"```\s*(cpp|c\+\+|cc|python|python3|py)\s*\n(.*?)```", response, re.I | re.S)
    if match:
        tag = match.group(1).lower()
        code = match.group(2)
    else:
        language_tags = r"(cpp|c\+\+|cc|python|python3|py)"
        open_fences = list(re.finditer(rf"```\s*{language_tags}\s*\n", response, re.I))
        unclosed = next(
            (candidate for candidate in reversed(open_fences)
             if "```" not in response[candidate.end():]),
            None,
        )
        if unclosed:
            tag, code = unclosed.group(1).lower(), response[unclosed.end():]
        else:
            # Recover a code body when a truncated response starts with its
            # language label but the model omitted the opening fence.
            match = re.match(rf"\s*{language_tags}\s*\n(.*)", response, re.I | re.S)
            if not match:
                raise ValueError("model response must contain a fenced cpp or python program")
            tag, code = match.group(1).lower(), match.group(2)
        first_code_line = next((line.strip() for line in code.splitlines() if line.strip()), "")
        cpp_start = r"(?:#include|using\b|int\b|void\b|auto\b|struct\b|class\b|template\b|const\b|long\b|std::|//|/\*)"
        python_start = r"(?:import\b|from\b|def\b|class\b|async\s+def\b|if\b|for\b|while\b|#|@)"
        expected = cpp_start if tag in {"cpp", "c++", "cc"} else python_start
        if not re.match(expected, first_code_line):
            raise ValueError("model response must contain a fenced cpp or python program")
    language = Language.CPP if tag in {"cpp", "c++", "cc"} else Language.PYTHON
    return language, code.strip() + "\n"


def _prepared_grade_files(problem: ProblemData, target: Path,
                          tests_path: Path | None = None) -> tuple[Path, Path]:
    tests_source = tests_path or problem.tests_path
    if tests_source is None:
        raise FileNotFoundError(f"no tests.jsonl found for {problem.id}; cannot grade")
    if problem.problem_json_path:
        problem_json = target / "problem.json"
        shutil.copyfile(problem.problem_json_path, problem_json)
        checker = Path(problem.problem_json_path).parent / "checker.py"
        if checker.is_file():
            shutil.copyfile(checker, target / "checker.py")
    else:
        problem_json = target / "problem.json"
        write_json(problem_json, {**problem.metadata, "id": problem.id, "year": problem.year,
                                  "day": problem.day, "title": problem.title,
                                  "statement": problem.statement,
                                  "time_limit_s": problem.time_limit_s,
                                  "memory_limit_mb": problem.memory_limit_mb})
    tests = target / "tests.jsonl"
    testdata = target / "test-data"
    rewritten: list[str] = []
    refs = ("input_path", "output_path", "expected_path", "input", "expected", "output", "stdin", "stdout")
    for index, line in enumerate(tests_source.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        record = json.loads(line)
        if isinstance(record, dict):
            for key in refs:
                value = record.get(key)
                if not isinstance(value, str):
                    continue
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = tests_source.parent / candidate
                if candidate.is_file():
                    testdata.mkdir(parents=True, exist_ok=True)
                    staged_name = f"{index}-{key}-{candidate.name}"
                    shutil.copyfile(candidate, testdata / staged_name)
                    record[key] = f"test-data/{staged_name}"
            rewritten.append(json.dumps(record, ensure_ascii=False))
        else:
            rewritten.append(line)
    tests.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return problem_json, tests


def _generate(model: str, prompt: str, timeout: float, max_tokens: int, tries: int = 3):
    for attempt in range(1, tries + 1):
        try:
            return run_model(model, [{"role": "user", "content": prompt}], None, timeout, max_tokens)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {408, 429} or exc.code >= 500
            if attempt == tries or not retryable:
                raise
            time.sleep(5 * attempt)


def _no_code_verdict(attempt_id: str, problem_id: str, env_id: str) -> Verdict:
    return Verdict(attempt_id=attempt_id, problem_id=problem_id, subtask_pass={}, all_pass=False,
                   partial_score=0.0, outcome=Outcome3.FAIL, env_id=env_id)


def run_one(problem: ProblemData, model: str, sample_index: int, *, out: Path, run_id: str,
            judge: Any, timeout: float, max_tokens: int | None = None, tests_path: Path | None = None,
            grade: bool = True) -> dict[str, Any]:
    """Generate once and judge it. With grade=False the code still runs on every test and each
    stdout is kept, but no verdict is produced: a checker grades those problems later."""
    prompt = build_single_prompt(problem)
    effective_max_tokens = resolve_max_tokens(model, max_tokens)
    generation = _generate(model, prompt, timeout, effective_max_tokens)
    generation_record = {**generation.to_record(), "max_tokens": effective_max_tokens}
    failure_kind = (
        FailureKind.TRUNCATED
        if (generation.stop_reason or "").lower() in {"length", "max_tokens", "max_output_tokens"}
        else None
    )
    artifact = attempt_dir(out, run_id, problem.id, Mode.SINGLE_SHOT.value, model, sample_index)
    artifact.mkdir(parents=True, exist_ok=True)
    record_statement_file(problem, artifact)
    (artifact / "prompt.txt").write_text(prompt, encoding="utf-8")
    (artifact / "raw_response.txt").write_text(generation.text, encoding="utf-8")
    try:
        language, code = extract_solution(generation.text)
    except ValueError as exc:
        attempt_id = f"{run_id}-{problem.id}-single-{sample_index}-{uuid.uuid4().hex[:8]}"
        transcript_path = artifact / "transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"role": "user", "content": prompt}, ensure_ascii=False) + "\n"
            + json.dumps({"role": "assistant", "content": generation.text,
                          "generation": generation_record}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        attempt = Attempt(
            id=attempt_id, problem_id=problem.id, mode=Mode.SINGLE_SHOT, model=model,
            provider="openrouter", language=None, env_id=run_env_id(judge),
            prompt_hash=sha256_text(prompt),
            transcript_path=str(transcript_path), code_path=None, sample_index=sample_index,
            generation={**generation_record, "extraction_error": str(exc)},
            failure_kind=failure_kind or FailureKind.NO_PROGRAM,
        )
        dump_attempt(artifact / "attempt.json", attempt)
        failure = {"attempt_id": attempt_id, "problem_id": problem.id, "all_pass": False,
                    "outcome": "fail", "failure_kind": (failure_kind or FailureKind.NO_PROGRAM).value,
                    "reason": str(exc), "judge_ran": False}
        if grade:
            write_json(artifact / "verdict.json", to_json(_no_code_verdict(attempt_id, problem.id, attempt.env_id)))
        else:
            write_json(artifact / "pending_checker.json", {
                "attempt_id": attempt_id, "problem_id": problem.id, "status": "pending_checker",
                "tests_run": 0, "reason": str(exc)})
            failure = {**failure, "outcome": "pending_checker"}
        append_result(out / run_id / "results.jsonl", attempt, failure)
        return {"attempt": asdict(attempt), "verdict": failure, "artifact": str(artifact)}
    code_path = artifact / ("solution.cpp" if language == Language.CPP else "solution.py")
    code_path.write_text(code, encoding="utf-8")
    attempt_id = f"{run_id}-{problem.id}-single-{sample_index}-{uuid.uuid4().hex[:8]}"
    attempt = Attempt(
        id=attempt_id, problem_id=problem.id, mode=Mode.SINGLE_SHOT, model=model,
        provider="openrouter", language=language, env_id=run_env_id(judge),
        prompt_hash=sha256_text(prompt),
        transcript_path=str(artifact / "transcript.jsonl"), code_path=str(code_path),
        sample_index=sample_index, generation=generation_record, failure_kind=failure_kind,
    )
    with (artifact / "transcript.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "user", "content": prompt}, ensure_ascii=False) + "\n")
        fh.write(json.dumps({"role": "assistant", "content": generation.text,
                             "generation": generation_record}, ensure_ascii=False) + "\n")
    dump_attempt(artifact / "attempt.json", attempt)
    problem_json, tests_jsonl = _prepared_grade_files(problem, artifact, tests_path)
    verdict = judge.grade(attempt_id=attempt_id, code_path=code_path, lang=language.value,
                          problem_json=problem_json, tests_jsonl=tests_jsonl,
                          out_dir=artifact / "judge", run_only=not grade)
    if grade:
        write_json(artifact / "verdict.json", verdict)
    else:
        shutil.copyfile(artifact / "judge/run_results.jsonl", artifact / "run_results.jsonl")
        write_json(artifact / "pending_checker.json", verdict)
    append_result(out / run_id / "results.jsonl", attempt, verdict)
    return {"attempt": asdict(attempt), "verdict": verdict, "artifact": str(artifact)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--problems", required=True, help="Comma-separated IDs or 'admitted'.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--judge", choices=("remote", "stub"), default="remote")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Override the model-specific OpenRouter output-token limit.")
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--judge-path", default=DEFAULT_JUDGE_PATH)
    parser.add_argument("--zips-root", default=DEFAULT_ZIPS_ROOT)
    parser.add_argument("--backend", choices=("vm", "host"), default="vm")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.k < 1:
        raise SystemExit("--k must be positive")
    problems = resolve_problems(args.problems, args.corpus)
    judge = StubJudge() if args.judge == "stub" else RemoteJudge(
        ssh_host=args.ssh_host, judge_path=args.judge_path, zips_root=args.zips_root,
        backend=args.backend)
    run_id = create_run_id()
    for problem_id in problems:
        problem = load_problem(problem_id, args.corpus)
        for sample_index in range(1, args.k + 1):
            result = run_one(problem, args.model, sample_index, out=args.out, run_id=run_id,
                             judge=judge, timeout=args.timeout, max_tokens=args.max_tokens)
            print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

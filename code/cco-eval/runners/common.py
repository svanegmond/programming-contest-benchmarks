"""Shared problem loading, prompts, artifacts, and judge selection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from admit import extract_samples
from cco_eval.corpus_cco import build_index, find_problem, iter_problems, parse_problem_html
from cco_eval.decoding import (
    DEFAULT_REASONING_EFFORT,
    REASONING_TOKEN_FLOOR,
    resolve_max_tokens,
    resolve_reasoning_effort,
    supported_parameters,
)
from cco_eval.providers import DEFAULT_OPENROUTER_URL, generate_openrouter
from cco_eval.utils import append_jsonl, write_json
from cco_eval.waterloo import Lane, to_json
from runners.judge_client import DEFAULT_JUDGE_PATH, DEFAULT_ZIPS_ROOT  # noqa: F401

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[3]
PROJECT_DIR = HERE.parents[1]
DEFAULT_CORPUS = REPO_ROOT / "statements"

DEFAULT_REASONING_MAX_TOKENS = REASONING_TOKEN_FLOOR


@dataclass
class ProblemData:
    id: str
    year: int
    day: int
    title: str
    statement: str
    statement_path: Path | None
    time_limit_s: float
    memory_limit_mb: int | None
    metadata: dict[str, Any]
    tests_path: Path | None
    problem_json_path: Path | None
    samples: list[tuple[str, str]] = field(default_factory=list)


def _repo_path(value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or REPO_ROOT) / path
    return path.resolve()


def _problem_id_matches(problem: dict[str, Any], requested: str) -> bool:
    aliases = {str(problem.get("id", "")), str(problem.get("benchmark_id", ""))}
    year = str(problem.get("year", ""))
    day = problem.get("day")
    day_problem = problem.get("day_problem")
    if year and isinstance(day, int) and isinstance(day_problem, int):
        aliases.add(f"cco-{year}-d{day}p{day_problem}")
        aliases.add(f"{year}-d{day}p{day_problem}")
    return requested in aliases


def load_problem(problem_id: str, corpus: Path = DEFAULT_CORPUS, problems_root: Path | None = None) -> ProblemData:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", problem_id):
        raise ValueError(f"invalid problem ID: {problem_id!r}")
    problem_root = problems_root or REPO_ROOT / "problems"
    folder = problem_root / problem_id
    problem_json = folder / "problem.json"
    admission = folder / "status.json"
    if problem_json.exists():
        raw = json.loads(problem_json.read_text(encoding="utf-8"))
        statement_value = raw.get("statement", "")
        statement_path = raw.get("statement_path")
        statement_file = None
        if statement_path:
            candidate = Path(statement_path).expanduser()
            if candidate.is_absolute():
                statement_file = candidate.resolve()
            else:
                repo_candidate = (REPO_ROOT / candidate).resolve()
                statement_file = repo_candidate if repo_candidate.is_file() else (folder / candidate).resolve()
        samples: list[tuple[str, str]] = []
        if not statement_value and statement_file and statement_file.exists():
            if statement_file.suffix.lower() in {".html", ".htm"}:
                statement_value, samples = _html_section(statement_file, str(raw.get("id", problem_id)), raw)
            else:
                statement_value = statement_file.read_text(encoding="utf-8", errors="replace")
        tests_value = raw.get("tests_path", raw.get("tests", "tests.jsonl"))
        tests_path = _repo_path(tests_value, base=folder) if isinstance(tests_value, str) else folder / "tests.jsonl"
        limit_value = raw.get("time_limit_s", raw.get("time_limit", _time_limit(str(statement_value))))
        try:
            time_limit = float(limit_value)
        except (TypeError, ValueError):
            time_limit = _time_limit(str(limit_value))
        memory_value = raw.get("memory_limit_mb")
        try:
            memory_limit = int(memory_value) if memory_value is not None else None
        except (TypeError, ValueError):
            memory_limit = _memory_limit(str(memory_value))
        tests_path = tests_path if tests_path.exists() else None
        if not samples and tests_path is not None:
            samples = _sample_rows(tests_path)
        return ProblemData(
            id=str(raw.get("id", problem_id)),
            year=int(raw.get("year", 0)), day=int(raw.get("day", 0)),
            title=str(raw.get("title", problem_id)), statement=str(statement_value),
            statement_path=statement_file, time_limit_s=time_limit,
            memory_limit_mb=memory_limit, metadata=raw,
            tests_path=tests_path, problem_json_path=problem_json, samples=samples,
        )

    index = build_index(corpus)
    problems = list(iter_problems(index))
    source = next((p for p in problems if _problem_id_matches(p, problem_id)), None)
    if source is None:
        try:
            source = find_problem(index, problem_id)
        except KeyError as exc:
            raise KeyError(f"problem not found in {problem_root} or corpus: {problem_id}") from exc
    html_path = _repo_path(str(source.get("raw_html_path", "")), base=corpus)
    statement_path: Path | None = None
    if html_path.is_file():
        # The canonical parsed statement is stored in the index; write it into the attempt.
        statement = str(source.get("statement", ""))
    else:
        statement = str(source.get("statement", ""))
    return ProblemData(
        id=str(source.get("id", problem_id)), year=int(source.get("year", 0)),
        day=int(source.get("day", 0)), title=str(source.get("title") or source.get("heading") or problem_id),
        statement=statement, statement_path=statement_path, time_limit_s=_time_limit(statement),
        memory_limit_mb=_memory_limit(statement), metadata=dict(source), tests_path=None, problem_json_path=None,
    )


def _html_section(html_path: Path, problem_id: str, raw: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    """Return only this problem's statement text and its samples from a whole-year problem-set page."""
    match = re.search(r"-d(\d+)p(\d+)$", problem_id)
    if not match:
        raise ValueError(f"cannot find the day and problem number in id {problem_id!r}")
    day, day_problem = int(match.group(1)), int(match.group(2))
    sections, _ = parse_problem_html(str(raw.get("year", "")), html_path)
    chosen = [p for p in sections if p["day"] == day and p["day_problem"] == day_problem]
    if len(chosen) != 1:
        raise ValueError(f"{html_path.name}: expected one section for day {day} problem {day_problem}, found {len(chosen)}")
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    return str(chosen[0]["statement"]), extract_samples(html_text, day, day_problem)


def _sample_rows(tests_path: Path) -> list[tuple[str, str]]:
    samples: list[tuple[str, str]] = []
    for line in tests_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not (record.get("sample") is True or str(record.get("kind", "")).lower() in {"sample", "example"}):
            continue
        in_value = record.get("input_path", record.get("input", record.get("stdin", "")))
        out_value = record.get("expected_path", record.get("output_path", record.get("expected", record.get("output", ""))))
        in_path, out_path = tests_path.parent / str(in_value), tests_path.parent / str(out_value)
        if in_path.is_file() and out_path.is_file():
            samples.append((in_path.read_text(encoding="utf-8"), out_path.read_text(encoding="utf-8")))
    return samples


def admitted_problem_ids() -> list[str]:
    ids: list[str] = []
    root = REPO_ROOT / "problems"
    if not root.is_dir():
        return ids
    for status_path in sorted(root.glob("*/status.json")):
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status.get("status") == "admitted":
            ids.append(status.get("problem_id", status_path.parent.name))
    return ids


def _time_limit(statement: str) -> float:
    match = re.search(r"Time\s+Limit\s*:\s*([\d.]+)\s*(ms|seconds?|s)?", statement, re.I)
    if not match:
        return 2.0
    value = float(match.group(1))
    return value / 1000 if (match.group(2) or "").lower() == "ms" else value


def _memory_limit(statement: str) -> int | None:
    match = re.search(r"Memory\s+Limit\s*:\s*([\d.]+)\s*(KB|MB|GB)?", statement, re.I)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "MB").upper()
    return int(value / 1024 if unit == "KB" else value * 1024 if unit == "GB" else value)


def language_block(lane: Lane) -> str:
    if lane is Lane.CPP:
        return (
            "Write GNU C++20 compiled with g++ (`-std=gnu++20`). "
            "`#include <bits/stdc++.h>` and the usual standard headers are available. "
            "Fast I/O (`ios::sync_with_stdio(false); cin.tie(nullptr);`) is recommended on tight limits. "
            "Tag the final fence `cpp`."
        )
    if lane is Lane.PYTHON:
        return (
            "Write Python 3, run with PyPy3. "
            "Fast I/O (`sys.stdin.buffer` / `sys.stdout.write`) is recommended on tight limits. "
            "Tag the final fence `python`."
        )
    if lane is Lane.FREE:
        return (
            "Choose exactly one language: GNU C++20 (g++) or Python 3 (PyPy3). "
            "Tag the final fence `cpp` or `python`. C++ is usually safer on tight olympiad limits. "
            "In C++, `#include <bits/stdc++.h>` and fast I/O (`ios::sync_with_stdio(false); cin.tie(nullptr);`) "
            "are available. In Python, prefer `sys.stdin.buffer` / `sys.stdout.write`."
        )
    raise ValueError(f"unknown lane: {lane}")


def fence_instruction(lane: Lane) -> str:
    if lane is Lane.CPP:
        return "tagged `cpp`"
    if lane is Lane.PYTHON:
        return "tagged `python`"
    if lane is Lane.FREE:
        return "tagged `cpp` or `python`"
    raise ValueError(f"unknown lane: {lane}")


def build_single_system_prompt(problem: ProblemData, lane: Lane = Lane.CPP) -> str:
    limits = f"Respect the time limit of {problem.time_limit_s:g} seconds"
    if problem.memory_limit_mb:
        limits += f" and memory limit of {problem.memory_limit_mb} MB"
    limits += "."
    return (
        "You are solving a published past Canadian Computing Olympiad (CCO) problem. "
        "The statement is public contest material used to evaluate models. "
        "A complete, compiling solution is required even if you are unsure.\n\n"
        f"{language_block(lane)}\n\n"
        "Use only the language standard library. Read input from stdin and write output to stdout. "
        f"{limits}\n\n"
        "You may write a short rationale. End with exactly one fenced program "
        f"{fence_instruction(lane)} — the final fenced block is the submission."
    )


def build_single_prompt(problem: ProblemData, lane: Lane = Lane.CPP) -> str:
    memory = f"\nMemory limit: {problem.memory_limit_mb} MB" if problem.memory_limit_mb else ""
    return (
        f"Problem: {problem.id} - {problem.title}\n\n"
        f"Time limit: {problem.time_limit_s:g} seconds{memory}\n\n"
        f"{problem.statement}\n"
    )


def build_single_messages(problem: ProblemData, lane: Lane = Lane.CPP) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": build_single_system_prompt(problem, lane)},
        {"role": "user", "content": build_single_prompt(problem, lane)},
    ]


def create_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", model).strip("-") or "model"


def attempt_dir(out: Path, run_id: str, problem_id: str, mode: str, model: str, sample_index: int) -> Path:
    return out.resolve() / run_id / problem_id / mode / model_slug(model) / f"s{sample_index}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_problems(value: str, corpus: Path) -> list[str]:
    ids = admitted_problem_ids() if value.strip().lower() == "admitted" else [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise ValueError("no problem IDs selected")
    for problem_id in ids:
        load_problem(problem_id, corpus)
    return ids


def run_model(
    model: str,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]] | None,
    timeout: float,
    max_tokens: int = DEFAULT_REASONING_MAX_TOKENS,
    *,
    temperature: float = 0.0,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    catalog: dict[str, Any] | None = None,
    tries: int = 3,
):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("missing OPENROUTER_API_KEY")
    api_model = model.removeprefix("openrouter/")
    effort = resolve_reasoning_effort(api_model, reasoning_effort, catalog)
    params = supported_parameters(api_model, catalog)
    last_exc: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return generate_openrouter(
                api_model, messages, api_key, timeout, max_tokens, tools=tools,
                base_url=DEFAULT_OPENROUTER_URL, reasoning_effort=effort,
                temperature=temperature, supported_parameters=params,
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {408, 429} or exc.code >= 500
            if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403, 404}:
                retryable = False
            if attempt == tries or not retryable:
                raise
            time.sleep(min(5 * attempt, 30))
    raise last_exc if last_exc else RuntimeError("model request failed")


def dump_attempt(path: Path, attempt: object) -> None:
    write_json(path, to_json(attempt))


def append_result(path: Path, attempt: object, verdict: dict[str, Any]) -> None:
    append_jsonl(path, {"attempt": to_json(attempt), "verdict": verdict})


def run_env_id(judge: Any) -> str:
    return str(judge.env_id())


def record_statement_file(problem: ProblemData, attempt_path: Path) -> Path:
    path = attempt_path / "problem-statement.md"
    path.write_text(problem.statement, encoding="utf-8")
    return path

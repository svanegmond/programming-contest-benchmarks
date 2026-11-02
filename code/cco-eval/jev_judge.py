#!/usr/bin/env python3
"""Measured judge: ask Jev whether a model's output is a valid answer to a CCO problem.

Jev never decides the official verdict. Rows written here are an extra signal
next to the judge's own pass/fail, used to study how far output-only validity
estimates can be trusted on any-valid problems.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runners.common import REPO_ROOT, load_problem

API_URL = "https://api.typesafe.ai/v1/systemone"
API_KEY_ENV = "TYPESAFE_API_KEY"
# jev-latest is an alias that moves; a pinned id keeps calibration reproducible.
DEFAULT_MODEL = "jev-1.14.0"
# Input tokens only; output is free (docs.typesafe.ai/models).
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000
# Default per-question context is 32k tokens; numeric text tokenizes badly, so stay well under it.
MAX_STATE_CHARS = 60_000

PROBLEMS_ROOT = REPO_ROOT / "problems"
CALIBRATION_DIR = REPO_ROOT / "jev-calibration"
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class Cause(StrEnum):
    VALID = "valid"
    WRONG_FORMAT = "wrong_format"
    VIOLATES_CONSTRAINT = "violates_constraint"
    SUBOPTIMAL = "suboptimal"
    WRONG_ANSWER = "wrong_answer"


CAUSE_CRITERIA: dict[str, str] = {
    Cause.VALID: "`model_output` is a correct output: it has the required format, obeys every rule in `problem_statement`, and is as good as the statement demands.",
    Cause.WRONG_FORMAT: "`model_output` does not have the output format `problem_statement` specifies: wrong number of lines or values, missing or extra text, or the wrong kind of value.",
    Cause.VIOLATES_CONSTRAINT: "`model_output` has the required format but breaks a rule that `problem_statement` places on correct outputs, such as a value out of range or an impossible combination.",
    Cause.SUBOPTIMAL: "`model_output` has the required format and obeys every rule, but is not the best value `problem_statement` asks for, such as a cost larger than the minimum.",
    Cause.WRONG_ANSWER: "`model_output` has the required format and seems to obey the rules, but its content is wrong for `test_input`, such as saying no solution exists when one does.",
}

P_VALID_INSTRUCTIONS = (
    "Using `problem_statement` and `test_input`, is `model_output` a correct output for this input? "
    "It is correct only if it has the output format `problem_statement` specifies, obeys every rule in it, "
    "and is optimal when the statement asks for a minimum or maximum. "
    "`reference_output` is one output the organizers accept. If `problem_statement` says any correct output is accepted, "
    "a `model_output` that differs from `reference_output` can still be correct. Otherwise `model_output` must equal `reference_output`."
)
CAUSE_INSTRUCTIONS = (
    "Using `problem_statement` and `test_input`, what is the main thing wrong with `model_output`, if anything? "
    "`reference_output` is one output the organizers accept. If `problem_statement` says any correct output is accepted, "
    "a different `model_output` can still be correct."
)

CASE_FIELDS = ("attempt_id", "test_id", "problem_id", "input", "expected", "output")


class MissingKeyError(RuntimeError):
    pass


class JevResponseError(RuntimeError):
    pass


class JevClient(Protocol):
    def system_one(self, *, model: str, state: dict[str, str], questions: dict[str, Any]) -> dict[str, Any]: ...


class HttpJevClient:
    """Talks to POST /v1/systemone directly; the request and response shapes are the documented OpenAPI ones."""

    def __init__(self, api_key: str | None = None, *, url: str = API_URL, timeout_s: float = 120.0, retries: int = 4) -> None:
        key = api_key or os.environ.get(API_KEY_ENV)
        if not key:
            raise MissingKeyError(
                f"{API_KEY_ENV} is not set. Create a key at https://console.typesafe.ai/ and export it, then rerun."
            )
        self._key, self._url, self._timeout, self._retries = key, url, timeout_s, retries

    def system_one(self, *, model: str, state: dict[str, str], questions: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
        request = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        for attempt in range(self._retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code not in RETRY_STATUSES or attempt == self._retries:
                    raise JevResponseError(f"HTTP {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2.0 ** attempt
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self._retries:
                    raise JevResponseError(f"connection failed: {exc}") from exc
                delay = 2.0 ** attempt
            time.sleep(delay)
        raise AssertionError("unreachable")


@dataclass
class JevResult:
    attempt_id: str
    test_id: str
    p_valid: float | None
    cause: Cause | None
    jev_model: str | None
    raw: dict[str, Any]


def build_questions() -> dict[str, Any]:
    return {
        "p_valid": {
            "type": "noul",
            "instructions": P_VALID_INSTRUCTIONS,
            "criteria": {
                "true": "`model_output` is a correct output for `test_input`.",
                "false": "`model_output` is not a correct output for `test_input`.",
            },
        },
        "cause": {"type": "choice", "instructions": CAUSE_INSTRUCTIONS, "criteria": dict(CAUSE_CRITERIA)},
    }


def build_state(statement: str, test_input: str, expected: str, output: str) -> dict[str, str]:
    return {"problem_statement": statement, "test_input": test_input, "reference_output": expected, "model_output": output}


def parse_response(response: dict[str, Any]) -> tuple[float, Cause, str]:
    try:
        answers = response["answers"]
        p_valid = float(answers["p_valid"]["noul"])
        cause = Cause(answers["cause"]["choice"])
        model = str(response["model"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JevResponseError(f"unexpected response shape: {exc!r}: {json.dumps(response)[:500]}") from exc
    if not 0.0 <= p_valid <= 1.0:
        raise JevResponseError(f"p_valid out of range: {p_valid}")
    return p_valid, cause, model


def judge_case(client: JevClient, case: dict[str, Any], *, statement: str, model: str = DEFAULT_MODEL,
               max_state_chars: int = MAX_STATE_CHARS) -> JevResult:
    attempt_id, test_id = str(case["attempt_id"]), str(case["test_id"])
    state = build_state(statement, str(case["input"]), str(case["expected"]), str(case["output"]))
    size = sum(len(v) for v in state.values())
    if size > max_state_chars:
        return JevResult(attempt_id, test_id, None, None, None, {"error": f"state_too_large: {size} chars > {max_state_chars}"})
    started = time.monotonic()
    try:
        response = client.system_one(model=model, state=state, questions=build_questions())
        p_valid, cause, jev_model = parse_response(response)
    except JevResponseError as exc:
        return JevResult(attempt_id, test_id, None, None, None, {"error": str(exc), "latency_s": time.monotonic() - started})
    raw = {**response, "latency_s": time.monotonic() - started}
    return JevResult(attempt_id, test_id, p_valid, cause, jev_model, raw)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def judge_cases(client: JevClient, cases: list[dict[str, Any]], results_path: Path, *, problems_root: Path = PROBLEMS_ROOT,
                model: str = DEFAULT_MODEL, workers: int = 4, max_state_chars: int = MAX_STATE_CHARS) -> list[JevResult]:
    """Append one row per case to results_path. Cases that already have an error-free row are skipped, so reruns resume."""
    for case in cases:
        missing = [f for f in CASE_FIELDS if f not in case]
        if missing:
            raise ValueError(f"case {case.get('attempt_id')}/{case.get('test_id')} is missing fields {missing}")
    done = {(r["attempt_id"], r["test_id"]) for r in read_jsonl(results_path) if "error" not in r["raw"]}
    todo = [c for c in cases if (c["attempt_id"], c["test_id"]) not in done]
    statements = {pid: load_problem(pid, problems_root=problems_root).statement for pid in {c["problem_id"] for c in todo}}

    def run(case: dict[str, Any]) -> JevResult:
        return judge_case(client, case, statement=statements[case["problem_id"]], model=model, max_state_chars=max_state_chars)

    results: list[JevResult] = []
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool, results_path.open("a", encoding="utf-8") as out:
        for result in pool.map(run, todo):
            out.write(json.dumps(asdict(result), sort_keys=True) + "\n")
            out.flush()
            results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, help="JSONL rows with attempt_id, test_id, problem_id, input, expected, output")
    parser.add_argument("--out", type=Path, default=Path("jev_results.jsonl"))
    parser.add_argument("--calibrate", action="store_true", help="run the calibration plan on the prepared corrupted-variant cases")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--problems-root", type=Path, default=PROBLEMS_ROOT)
    args = parser.parse_args(argv)

    if args.calibrate:
        args.cases = args.cases or CALIBRATION_DIR / "cases.jsonl"
        args.out = CALIBRATION_DIR / "jev_results.jsonl" if args.out == Path("jev_results.jsonl") else args.out
    if args.cases is None:
        parser.error("--cases is required unless --calibrate is given")
    if not args.cases.exists():
        print(f"{args.cases} does not exist. For calibration run: python jev_calibration.py prep (needs the CCO test ZIPs).", file=sys.stderr)
        return 2
    try:
        client = HttpJevClient()
    except MissingKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cases = read_jsonl(args.cases)
    results = judge_cases(client, cases, args.out, problems_root=args.problems_root, model=args.model, workers=args.workers)
    errors = [r for r in results if r.p_valid is None]
    print(f"judged {len(results) - len(errors)} cases, {len(errors)} errors, wrote {args.out}")
    if args.calibrate:
        from jev_calibration import summarize, write_summary
        summary = summarize(cases, read_jsonl(args.out))
        write_summary(summary, CALIBRATION_DIR / "summary.json")
        print(json.dumps(summary, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

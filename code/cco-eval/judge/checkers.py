"""Load and run per-problem checkers (grading kinds `validator` and `scorer`).

A checker is `problems/<id>/checker.py` exposing
`check(input_text, expected_text, output_text) -> {ok, score, message}`. It runs
as plain Python in the judge's own process on the grading host. It never
executes submission code: the submission only ever ran inside the sandbox/VM,
and its stdout reaches the checker as text.
"""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CHECKER_FILENAME = "checker.py"
CHECKER_RUNS_IN = "host_python"


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    score: float
    message: str


@dataclass(frozen=True)
class Checker:
    path: Path
    sha256: str
    check: Callable[[str, str, str], dict]


def load_checker(problem_dir: Path) -> Checker:
    path = problem_dir / CHECKER_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"grading kind needs a checker but {path} does not exist")
    spec = importlib.util.spec_from_file_location(f"waterloo_checker_{problem_dir.name.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import checker {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    check = getattr(module, "check", None)
    if not callable(check):
        raise ImportError(f"{path} does not define check(input_text, expected_text, output_text)")
    return Checker(path, hashlib.sha256(path.read_bytes()).hexdigest(), check)


def run_checker(checker: Checker, input_bytes: bytes, expected_bytes: bytes, output_bytes: bytes) -> CheckResult:
    try:
        output_text = output_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return CheckResult(False, 0.0, "output is not valid UTF-8")
    try:
        raw = checker.check(input_bytes.decode("utf-8"), expected_bytes.decode("utf-8"), output_text)
        score = float(raw["score"])
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"score {score} is outside [0, 1]")
        return CheckResult(bool(raw["ok"]), score, str(raw["message"]))
    except Exception as error:
        return CheckResult(False, 0.0, f"checker_error: {type(error).__name__}: {error}")

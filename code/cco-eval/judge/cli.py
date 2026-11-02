from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .core import env_id, grade, run_single


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m judge")
    parser.add_argument("--backend", choices=("vm", "host"), default="vm")
    commands = parser.add_subparsers(dest="command", required=True)
    env = commands.add_parser("env-id")
    env.add_argument("--backend", choices=("vm", "host"), default=argparse.SUPPRESS)

    run = commands.add_parser("run")
    run.add_argument("--backend", choices=("vm", "host"), default=argparse.SUPPRESS)
    run.add_argument("--lang", choices=("cpp", "python"), required=True)
    run.add_argument("--code", type=Path, required=True)
    run.add_argument("--stdin", type=Path, required=True)
    run.add_argument("--time-limit", type=float, required=True)
    run.add_argument("--memory-mb", type=int)
    run.add_argument("--kill-factor", type=float, default=3)

    grading = commands.add_parser("grade")
    grading.add_argument("--backend", choices=("vm", "host"), default=argparse.SUPPRESS)
    grading.add_argument("--code", type=Path, required=True)
    grading.add_argument("--lang", choices=("cpp", "python"), required=True)
    grading.add_argument("--problem", type=Path, required=True)
    grading.add_argument("--tests", type=Path, required=True)
    grading.add_argument("--zips-root", type=Path, required=True)
    grading.add_argument("--attempt-id", required=True)
    grading.add_argument("--out", type=Path, required=True)
    grading.add_argument("--run-only", action="store_true",
                          help="Run every test and retain stdout without comparing outputs.")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "env-id":
            result = {"env_id": env_id(args.backend)}
        elif args.command == "run":
            result = run_single(
                args.backend,
                args.lang,
                args.code,
                args.stdin,
                args.time_limit,
                args.memory_mb,
                args.kill_factor,
            )
        else:
            result = grade(
                args.backend,
                args.code,
                args.lang,
                args.problem,
                args.tests,
                args.zips_root,
                args.attempt_id,
                args.out,
                run_only=args.run_only,
            )
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}), file=sys.stderr)
        raise SystemExit(2) from error

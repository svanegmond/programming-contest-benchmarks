#!/usr/bin/env python3
"""Draw the pilot cohort: seeded random problems from the deterministic pool plus a few without a checker."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROBLEMS = ROOT / "problems"
DEFAULT_OUT = ROOT / "cohorts/cohort.json"
DETERMINISTIC_KINDS = {"exact", "float_tol"}
UNUSUAL_REASONS = {"needs_validator", "needs_scorer"}


def pools(problems_root: Path) -> tuple[list[str], list[str]]:
    deterministic: list[str] = []
    unusual: list[str] = []
    for status_path in sorted(problems_root.glob("*/status.json")):
        status = json.loads(status_path.read_text(encoding="utf-8"))
        problem = json.loads(status_path.with_name("problem.json").read_text(encoding="utf-8"))
        if status["status"] == "admitted" and problem["grading_kind"] in DETERMINISTIC_KINDS:
            deterministic.append(status["problem_id"])
        elif status["status"] == "rejected" and status["reason"] in UNUSUAL_REASONS:
            unusual.append(status["problem_id"])
    return deterministic, unusual


def draw(problems_root: Path, seed: int, deterministic_count: int, unusual_count: int) -> dict[str, object]:
    deterministic, unusual = pools(problems_root)
    rng = random.Random(seed)
    return {
        "seed": seed,
        "method": "random.Random(seed).sample over problem ids sorted ascending, deterministic pool first, then unusual pool, from the same generator",
        "deterministic_pool_size": len(deterministic),
        "unusual_pool_size": len(unusual),
        "deterministic": sorted(rng.sample(deterministic, deterministic_count)),
        "unusual": sorted(rng.sample(unusual, unusual_count)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--problems-root", type=Path, default=DEFAULT_PROBLEMS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--deterministic", type=int, default=10)
    parser.add_argument("--unusual", type=int, default=4)
    args = parser.parse_args()
    cohort = draw(args.problems_root, args.seed, args.deterministic, args.unusual)
    args.out.write_text(json.dumps(cohort, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(cohort, indent=2))


if __name__ == "__main__":
    main()

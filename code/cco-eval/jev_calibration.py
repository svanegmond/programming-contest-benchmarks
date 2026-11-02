#!/usr/bin/env python3
"""Build and summarize the Jev calibration set. Standard library only so `prep` runs where the test ZIPs live.

Each source test yields the official expected output plus corrupted variants of it.
Corruptions are chosen per problem so that they are provably invalid where the
statement allows a cheap check; `ground_truth_verified` records whether that check ran.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import zipfile
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CALIBRATION_DIR = REPO_ROOT / "jev-calibration"
PROBLEMS_ROOT = REPO_ROOT / "problems"
CALIBRATION_PROBLEMS = ("cco-2022-d1p2", "cco-2025-d2p2")
TESTS_PER_PROBLEM = 3
MAX_CASE_CHARS = 20_000
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000

# (output text, note, whether the corruption was verified invalid by code: True, or None when it could not be checked)
Mutation = tuple[str, str, bool | None]


def _lines(text: str) -> list[str]:
    return text.strip("\n").split("\n")


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def truncate(expected: str) -> Mutation:
    lines = _lines(expected)
    kept = lines[: max(1, len(lines) // 2)]
    return _join(kept), f"kept first {len(kept)} of {len(lines)} lines", len(kept) < len(lines)


def rainy_markets_mutations(test_input: str, expected: str) -> dict[str, Mutation]:
    """cco-2022-d1p2: line 2 is the umbrella cost, then one row per market (to shelter i, umbrellas, to shelter i+1)."""
    in_lines = _lines(test_input)
    people = [int(v) for v in in_lines[2].split()]
    lines = _lines(expected)
    rows = [[int(v) for v in line.split()] for line in lines[2:]]
    consistent = (
        lines[0] == "YES"
        and len(rows) == len(people)
        and all(sum(r) == p for r, p in zip(rows, people))
        and sum(r[1] for r in rows) == int(lines[1])
    )
    cost_changed = [lines[0], str(int(lines[1]) + 1), *lines[2:]]
    negative = [r[:] for r in rows]
    shift = negative[0][0] + 1
    negative[0][0], negative[0][2] = -1, negative[0][2] + shift
    out = {
        "truncated": truncate(expected),
        "value_changed": (_join(cost_changed), "umbrella cost line increased by 1, rows unchanged", consistent),
        "constraint_violated": (
            _join([lines[0], lines[1], *(" ".join(map(str, r)) for r in negative)]),
            "first market sends -1 people to shelter 1 and the surplus to shelter 2, row sum kept",
            consistent,
        ),
    }
    return out


def _connected(n: int, edges: list[tuple[int, int]]) -> bool:
    parent = list(range(n + 1))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for u, v in edges:
        parent[find(u)] = find(v)
    return len({find(i) for i in range(1, n + 1)}) == 1


def patrol_robot_mutations(test_input: str, expected: str) -> dict[str, Mutation]:
    """cco-2025-d2p2: M, then M segments u v over N points. The painted graph must be connected to visit every point."""
    n = int(_lines(test_input)[0])
    lines = _lines(expected)
    edges = [tuple(int(v) for v in line.split()) for line in lines[1:]]
    ok = int(lines[0]) == len(edges) and _connected(n, edges)  # type: ignore[arg-type]
    changed: Mutation = (expected, "no endpoint change disconnects the graph", None)
    for j in range(len(edges) - 1, -1, -1):
        for side in (1, 0):
            for w in range(1, n + 1):
                if w == edges[j][1 - side] or w == edges[j][side]:
                    continue
                trial = [list(e) for e in edges]
                trial[j][side] = w
                if not _connected(n, [tuple(e) for e in trial]):  # type: ignore[misc]
                    text = _join([lines[0], *(f"{u} {v}" for u, v in trial)])
                    changed = (text, f"segment {j + 1} endpoint {side + 1} changed to {w}, graph disconnected", ok)
                    break
            else:
                continue
            break
        else:
            continue
        break
    loop = _join([str(int(lines[0]) + 1), *lines[1:], "1 1"])
    return {
        "truncated": truncate(expected),
        "value_changed": changed,
        "constraint_violated": (loop, "added a segment from location 1 to itself (u_i != v_i is required)", True),
    }


MUTATORS: dict[str, Callable[[str, str], dict[str, Mutation]]] = {
    "cco-2022-d1p2": rainy_markets_mutations,
    "cco-2025-d2p2": patrol_robot_mutations,
}


def read_zip_member(zips_root: Path, ref: str) -> str:
    zip_name, member = ref.split("::", 1)
    with zipfile.ZipFile(zips_root / zip_name) as archive:
        return archive.read(member).decode("utf-8", errors="replace")


def select_tests(problem_id: str, tests: list[dict[str, Any]], zips_root: Path, count: int = TESTS_PER_PROBLEM) -> list[dict[str, Any]]:
    """Tests whose corruptions are all verified invalid, spread from smallest to largest so the picks cover different sizes.

    Tests over MAX_CASE_CHARS are skipped: input, reference and output together must fit Jev's per-question context.
    """
    candidates = []
    for row in tests:
        test_input = read_zip_member(zips_root, row["input_path"])
        expected = read_zip_member(zips_root, row["expected_path"])
        size = len(test_input) + 2 * len(expected)
        if size > MAX_CASE_CHARS or len(_lines(expected)) < 3:
            continue
        mutations = MUTATORS[problem_id](test_input, expected)
        if all(verified is True for _, _, verified in mutations.values()):
            candidates.append({**row, "input": test_input, "expected": expected, "size": size})
    candidates.sort(key=lambda c: (c["size"], c["input_path"]))
    if len(candidates) <= count:
        return candidates
    picks = sorted({round(i * (len(candidates) - 1) / (count - 1)) for i in range(count)})
    return [candidates[i] for i in picks]


def build_cases(problem_id: str, tests: list[dict[str, Any]], zips_root: Path) -> list[dict[str, Any]]:
    cases = []
    for test in select_tests(problem_id, tests, zips_root):
        test_id = Path(test["input_path"].split("::", 1)[1]).stem
        variants: dict[str, tuple[str, str, bool | None]] = {
            "expected": (test["expected"], "official expected output", True),
            **MUTATORS[problem_id](test["input"], test["expected"]),
        }
        for variant, (output, note, verified) in variants.items():
            cases.append({
                "attempt_id": f"calib-{problem_id}-{variant}",
                "test_id": test_id,
                "problem_id": problem_id,
                "input": test["input"],
                "expected": test["expected"],
                "output": output,
                "variant": variant,
                "ground_truth": "valid" if variant == "expected" else "invalid",
                "ground_truth_verified": verified,
                "note": note,
            })
    return cases


def prep(zips_root: Path, problems_root: Path, out: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for problem_id in CALIBRATION_PROBLEMS:
        tests = [json.loads(line) for line in (problems_root / problem_id / "tests.jsonl").read_text().splitlines() if line.strip()]
        cases += build_cases(problem_id, tests, zips_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(c, sort_keys=True) + "\n" for c in cases), encoding="utf-8")
    return cases


def _quantiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {"n": len(ordered), "min": ordered[0], "median": statistics.median(ordered), "mean": statistics.fmean(ordered), "max": ordered[-1]}


def summarize(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Join results to cases; when a case has several rows the last error-free one wins."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in results:
        key = (row["attempt_id"], row["test_id"])
        if "error" not in row["raw"] or key not in by_key:
            by_key[key] = row
    variants: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []
    tokens = 0
    errors = 0
    for case in cases:
        row = by_key.get((case["attempt_id"], case["test_id"]))
        entry = variants.setdefault(case["variant"], {"p_valid": [], "causes": {}, "errors": 0})
        if row is None or row["p_valid"] is None:
            entry["errors"] += 1
            errors += 1
            continue
        entry["p_valid"].append(row["p_valid"])
        entry["causes"][row["cause"]] = entry["causes"].get(row["cause"], 0) + 1
        latencies.append(row["raw"]["latency_s"])
        tokens += row["raw"]["usage"]["input_tokens"]
    valid_p = variants.get("expected", {}).get("p_valid", [])
    invalid_p = [p for v, e in variants.items() if v != "expected" for p in e["p_valid"]]
    return {
        "variants": {v: {**e, "p_valid_stats": _quantiles(e["p_valid"]) if e["p_valid"] else None} for v, e in variants.items()},
        "separation_at_0.5": {
            "valid_accepted": sum(p >= 0.5 for p in valid_p),
            "valid_total": len(valid_p),
            "invalid_rejected": sum(p < 0.5 for p in invalid_p),
            "invalid_total": len(invalid_p),
        },
        "errors": errors,
        "latency_s": _quantiles(latencies) if latencies else None,
        "input_tokens": tokens,
        "cost_usd": tokens * USD_PER_INPUT_TOKEN,
    }


def write_summary(summary: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prep", help="write cases.jsonl from the CCO test ZIPs")
    p.add_argument("--zips", type=Path, default=Path(os.environ.get("WATERLOO_ZIPS_ROOT", "/srv/waterloo/zips")))
    p.add_argument("--problems-root", type=Path, default=PROBLEMS_ROOT)
    p.add_argument("--out", type=Path, default=CALIBRATION_DIR / "cases.jsonl")
    args = parser.parse_args()
    cases = prep(args.zips, args.problems_root, args.out)
    print(f"wrote {len(cases)} cases to {args.out}")


if __name__ == "__main__":
    main()

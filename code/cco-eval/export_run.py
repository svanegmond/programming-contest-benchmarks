#!/usr/bin/env python3
"""Reduce a finished run directory to the publishable record: manifest.json, results.csv, summary.md.

Only identifiers, verdicts, outcomes and costs are copied. Code, transcripts and prompts never are.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cco_eval.run_records import load_run  # noqa: E402

MANIFEST_FIELDS = ("models", "mode", "caps", "budget", "problem_ids", "timeout_s")
CSV_FIELDS = ("problem", "mode", "model", "sample", "verdict", "outcome", "partial_score", "cost_usd")
OUTCOMES = ("pass_on_time", "pass_over_time", "truncated", "fail")


def costs(run_dir: Path) -> dict[str, float | None]:
    path = run_dir / "telemetry.jsonl"
    if not path.is_file():
        return {}
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {row["attempt_id"]: row.get("cost_usd") for row in rows}


def result_rows(run_dir: Path) -> list[dict[str, object]]:
    cost_by_attempt = costs(run_dir)
    rows = []
    for record in load_run(run_dir):
        attempt, verdict = record.attempt, record.verdict
        outcome = "truncated" if attempt.failure_kind == "truncated" else verdict.outcome
        rows.append({
            "problem": attempt.problem_id, "mode": str(attempt.mode), "model": attempt.model,
            "sample": attempt.sample_index, "verdict": "pass" if verdict.all_pass else "fail",
            "outcome": outcome, "partial_score": verdict.partial_score,
            "cost_usd": cost_by_attempt.get(attempt.id),
        })
    return sorted(rows, key=lambda r: (str(r["model"]), str(r["problem"]), int(r["sample"])))


def summary(label: str, manifest: dict[str, object], rows: list[dict[str, object]]) -> str:
    by_model: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_model[str(row["model"])].append(row)
    lines = [
        f"# {manifest['run_id']} ({label})", "",
        f"Mode `{manifest['mode']}`, {len(manifest['problem_ids'])} problems, {len(rows)} attempts.", "",
        "| Model | Attempts | On-time | Over-time | Truncated | Fail | Cost (USD) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model, group in sorted(by_model.items()):
        counts = Counter(str(r["outcome"]) for r in group)
        known = [float(r["cost_usd"]) for r in group if r["cost_usd"] is not None]
        cost = f"{sum(known):.6f}" if len(known) == len(group) else "n/a"
        lines.append(f"| `{model}` | {len(group)} | " + " | ".join(str(counts[o]) for o in OUTCOMES) + f" | {cost} |")
    return "\n".join(lines) + "\n"


def export(run_dir: Path, out: Path, label: str, note: str | None) -> None:
    raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest = {"run_id": raw["run_id"], "label": label, **{k: raw.get(k) for k in MANIFEST_FIELDS}}
    if note:
        manifest["note"] = note
    rows = result_rows(run_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with (out / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    (out / "summary.md").write_text(summary(label, manifest, rows), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Run directory holding run.json and attempt folders.")
    parser.add_argument("--out", type=Path, required=True, help="Destination folder, runs/<date>-<name>.")
    parser.add_argument("--label", required=True, help="For example: test, pilot, full.")
    parser.add_argument("--note", help="One line for manifest.json.")
    args = parser.parse_args(argv)
    export(args.run_dir, args.out, args.label, args.note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Reduce a finished run directory to the publishable record.

Writes run-config.json, results.csv, summary.md and index.html. Code, transcripts
and prompts are never copied. Publish the folder to the gh-pages branch, not main.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cco_eval.pages import CSV_FIELDS, publish  # noqa: E402

MANIFEST_FIELDS = (
    "models", "mode", "caps", "budget", "problem_ids", "timeout_s",
    "k", "temperature", "reasoning_effort", "lane", "selection", "prompt_version",
    "git_sha",
)


def export(run_dir: Path, out: Path, label: str, note: str | None) -> None:
    publish(run_dir, out)
    config_path = out / "run-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["label"] = label
    if note:
        config["note"] = note
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Run directory holding run.json and attempt folders.")
    parser.add_argument("--out", type=Path, required=True, help="Destination folder for the Pages report.")
    parser.add_argument("--label", required=True, help="For example: test, pilot, full.")
    parser.add_argument("--note", help="One line for run-config.json.")
    args = parser.parse_args(argv)
    export(args.run_dir, args.out, args.label, args.note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

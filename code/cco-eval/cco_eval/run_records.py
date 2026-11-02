"""Load a finished run directory: one attempt and its verdict per record."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cco_eval.waterloo import Attempt, Budget, Verdict


@dataclass(frozen=True)
class RunRecord:
    attempt: Attempt
    verdict: Verdict
    sample_index: int


def load_run(run_dir: Path) -> list[RunRecord]:
    """Attempts with a verdict.json beside attempt.json; pending_checker attempts have none by design."""
    records = []
    for attempt_path in sorted(run_dir.rglob("attempt.json")):
        verdict_path = attempt_path.with_name("verdict.json")
        if not verdict_path.exists():
            if attempt_path.with_name("pending_checker.json").exists():
                continue
            raise ValueError(f"Missing verdict.json beside {attempt_path}")
        attempt_data = json.loads(attempt_path.read_text())
        verdict_data = json.loads(verdict_path.read_text())
        if attempt_data.get("budget") is not None:
            attempt_data["budget"] = Budget(**attempt_data["budget"])
        attempt = Attempt(**attempt_data)
        verdict = Verdict(**verdict_data)
        if verdict.attempt_id != attempt.id or verdict.problem_id != attempt.problem_id:
            raise ValueError(f"Attempt/verdict identity mismatch in {verdict_path}")
        if verdict.env_id != attempt.env_id:
            raise ValueError(f"Attempt/verdict environment mismatch in {verdict_path}")
        records.append(RunRecord(attempt, verdict, attempt.sample_index))
    if not records:
        raise ValueError(f"No verdict.json files under {run_dir}")
    return records

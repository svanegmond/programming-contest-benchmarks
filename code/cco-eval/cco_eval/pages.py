"""Concise GitHub Pages report and a downloadable run-config payload."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from cco_eval.metrics import summarize_groups
from cco_eval.run_records import load_run
from cco_eval.utils import write_json

CSV_FIELDS = (
    "problem", "mode", "model", "sample", "language", "verdict", "outcome", "partial_score", "cost_usd",
)


def public_run_config(manifest: dict[str, Any]) -> dict[str, Any]:
    budget = manifest.get("budget") or {}
    caps = dict(manifest.get("caps") or {})
    return {
        "run_id": manifest.get("run_id"),
        "git_sha": manifest.get("git_sha"),
        "env_id": manifest.get("env_id"),
        "cohort_hash": manifest.get("cohort_hash"),
        "models": manifest.get("models"),
        "provider": "openrouter",
        "mode": manifest.get("mode"),
        "reasoning_effort": manifest.get("reasoning_effort"),
        "reasoning_effort_sent": caps.get("reasoning_effort"),
        "max_tokens": caps.get("max_tokens"),
        "temperature": manifest.get("temperature"),
        "k": manifest.get("k", 1),
        "lane": manifest.get("lane"),
        "selection": manifest.get("selection"),
        "prompt_version": manifest.get("prompt_version"),
        "max_turns": budget.get("max_turns") if isinstance(budget, dict) else None,
        "http_timeout_s": manifest.get("timeout_s"),
        "give_up_timeout_s": caps.get("give_up_timeout_s"),
        "multi_submit": manifest.get("multi_submit", False),
        "problem_ids": manifest.get("problem_ids"),
        "backend": caps.get("backend"),
    }


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
        language = attempt.language
        outcome = "truncated" if attempt.failure_kind == "truncated" else verdict.outcome
        rows.append({
            "problem": attempt.problem_id, "mode": str(attempt.mode), "model": attempt.model,
            "sample": attempt.sample_index,
            "language": str(language) if language else "",
            "verdict": "pass" if verdict.all_pass else "fail",
            "outcome": outcome, "partial_score": verdict.partial_score,
            "cost_usd": cost_by_attempt.get(attempt.id),
        })
    return sorted(rows, key=lambda row: (str(row["model"]), str(row["problem"]), int(row["sample"])))


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def markdown_report(manifest: dict[str, Any], run_dir: Path, *, include_download: bool = True) -> str:
    records = load_run(run_dir)
    k = int(manifest.get("k") or 1)
    groups = summarize_groups(records, k)
    lines = [
        f"# {manifest.get('run_id', run_dir.name)}",
        "",
        f"mode `{manifest.get('mode')}` · lane `{manifest.get('lane')}` · k `{k}` · "
        f"selection `{manifest.get('selection')}` · effort `{manifest.get('reasoning_effort')}` · "
        f"temp `{manifest.get('temperature')}` · prompt `{manifest.get('prompt_version')}` · "
        f"git `{manifest.get('git_sha')}`",
        "",
        "pass@k is correctness (`all_pass`). On-time / over-time are vs the statement limit, not the score.",
        "",
        "| Model | Language | pass@k | mean partial_score | On-time | Over-time | Truncated | Fail |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in groups:
        lines.append(
            f"| `{row['model']}` | {row['language']} | {_fmt(row['pass_at_k'])} | "
            f"{_fmt(row['mean_partial_score'])} | {row['on_time']} | {row['over_time']} | "
            f"{row['truncated']} | {row['fail']} |"
        )
    if include_download:
        lines.extend(["", "[Download run-config.json](run-config.json)"])
    lines.append("")
    return "\n".join(lines)


def render_html(manifest: dict[str, Any], run_dir: Path) -> str:
    records = load_run(run_dir)
    k = int(manifest.get("k") or 1)
    groups = summarize_groups(records, k)
    title = html.escape(str(manifest.get("run_id", run_dir.name)))
    meta = (
        f"mode {manifest.get('mode')} · lane {manifest.get('lane')} · k {k} · "
        f"selection {manifest.get('selection')} · effort {manifest.get('reasoning_effort')} · "
        f"temp {manifest.get('temperature')} · prompt {manifest.get('prompt_version')} · "
        f"git {manifest.get('git_sha')}. "
        "pass@k is correctness (all_pass); on-time / over-time are vs the statement limit."
    )
    rows = []
    for row in groups:
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(row['model']))}</code></td>"
            f"<td>{html.escape(str(row['language']))}</td>"
            f"<td>{html.escape(_fmt(row['pass_at_k']))}</td>"
            f"<td>{html.escape(_fmt(row['mean_partial_score']))}</td>"
            f"<td>{row['on_time']}</td><td>{row['over_time']}</td>"
            f"<td>{row['truncated']}</td><td>{row['fail']}</td>"
            "</tr>"
        )
    body = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
</head>
<body>
<h1>{title}</h1>
<p>{html.escape(meta)}</p>
<p><a href="run-config.json">Download run-config.json</a></p>
<table>
<thead>
<tr><th>Model</th><th>Language</th><th>pass@k</th><th>mean partial_score</th>
<th>On-time</th><th>Over-time</th><th>Truncated</th><th>Fail</th></tr>
</thead>
<tbody>
{body}
</tbody>
</table>
</body>
</html>
"""


def publish(run_dir: Path, out: Path) -> None:
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run-config.json", public_run_config(manifest))
    rows = result_rows(run_dir)
    with (out / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    (out / "summary.md").write_text(markdown_report(manifest, run_dir) + "\n", encoding="utf-8")
    (out / "index.html").write_text(render_html(manifest, run_dir), encoding="utf-8")

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
        "batch_models": caps.get("batch_models", []),
    }


BATCH_PUBLIC_FIELDS = (
    "batch_id", "model", "submit_model", "endpoint", "provider", "status", "submitted_at",
    "completed_at", "latency_s", "request_counts", "usage",
)


def batch_jobs(run_dir: Path) -> list[dict[str, Any]]:
    """Finished Batch API jobs recorded for a run (job ids, timestamps, usage, cost)."""
    jobs = []
    for path in sorted((run_dir / "batches").glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        jobs.append({key: state.get(key) for key in BATCH_PUBLIC_FIELDS})
    return jobs


def telemetry_rows(run_dir: Path) -> dict[str, dict[str, Any]]:
    path = run_dir / "telemetry.jsonl"
    if not path.is_file():
        return {}
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {row["attempt_id"]: row for row in rows}


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
    config = public_run_config(manifest)
    jobs = batch_jobs(run_dir)
    if jobs:
        config["batches"] = jobs
    write_json(out / "run-config.json", config)
    rows = result_rows(run_dir)
    with (out / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    (out / "summary.md").write_text(markdown_report(manifest, run_dir) + "\n", encoding="utf-8")
    (out / "index.html").write_text(render_html(manifest, run_dir), encoding="utf-8")


def _tests_passed(attempt_path: Path) -> str:
    results = attempt_path.parent / "judge" / "test_results.jsonl"
    if not results.is_file():
        return "-"
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
    return f"{sum(1 for r in rows if r.get('verdict') == 'pass')}/{len(rows)}"


def overview_rows(run_dir: Path) -> list[dict[str, str]]:
    telemetry = telemetry_rows(run_dir)
    rows = []
    for attempt_path in sorted(run_dir.rglob("attempt.json")):
        verdict_path = attempt_path.with_name("verdict.json")
        if not verdict_path.is_file():
            continue
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        t = telemetry.get(attempt["id"], {})
        generation = attempt.get("generation") or {}
        batch = generation.get("batch") or {}
        out_tokens = t.get("output_tokens")
        reasoning = t.get("reasoning_tokens")
        tokens = "-" if out_tokens is None else f"{out_tokens:,}" + (f" ({reasoning:,})" if reasoning else "")
        latency = t.get("latency_ms")
        cost = t.get("cost_usd")
        outcome = verdict.get("outcome", "")
        if not attempt.get("code_path"):
            outcome = f"no program ({generation.get('stop_reason') or 'none'})"
        rows.append({
            "run": run_dir.name, "problem": attempt["problem_id"], "model": attempt["model"].removeprefix("openrouter/"),
            "outcome": str(outcome), "tests": _tests_passed(attempt_path), "tokens": tokens,
            "time": "-" if latency is None else f"{latency / 1000:.0f} s",
            "cost": "-" if cost is None else f"${cost:.3f}",
            "api": "batch" if batch else "sync",
        })
    return sorted(rows, key=lambda r: (r["problem"], r["model"]))


OVERVIEW_COLUMNS = (("problem", "Problem"), ("model", "Model"), ("api", "API"), ("outcome", "Outcome"),
                    ("tests", "Tests passed"), ("tokens", "Output tokens (reasoning)"), ("time", "Time"),
                    ("cost", "Cost"))


def publish_site(run_dirs: list[Path], out: Path, notes: list[str] | None = None,
                 title: str = "CCO benchmark runs") -> None:
    """One page per run under ``out/<run_id>/`` plus a single overview ``index.html``."""
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for run_dir in run_dirs:
        publish(run_dir, out / run_dir.name)
        rows.extend(overview_rows(run_dir))
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in OVERVIEW_COLUMNS) + "<th>Run</th>"
    body = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(row[key])}</td>" for key, _ in OVERVIEW_COLUMNS)
        + f'<td><a href="{html.escape(row["run"])}/">{html.escape(row["run"])}</a> · '
        f'<a href="{html.escape(row["run"])}/run-config.json">config</a></td></tr>'
        for row in rows)
    note_html = "".join(f"<li>{html.escape(note)}</li>" for note in notes or [])
    (out / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:72rem;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<table>
<thead><tr>{head}</tr></thead>
<tbody>
{body}
</tbody>
</table>
<ul>{note_html}</ul>
<p>Each run links its run-config.json (models, caps, git SHA, batch job ids). Time is model latency
(for batch: submit to finalize). Cost is what OpenRouter reported.</p>
</body>
</html>
""", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

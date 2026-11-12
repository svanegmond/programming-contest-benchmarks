"""Orchestrate admission, cohort selection, and the runners."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from build_cohort import draw
from build_dashboard import _main_admitted_count, _records, metric_line, render_dashboard
from cco_eval import batch as batch_api
from cco_eval.decoding import (
    DEFAULT_HTTP_TIMEOUT_S,
    DEFAULT_REASONING_EFFORT,
    PROMPT_VERSION,
    load_openrouter_catalog,
    resolve_max_tokens,
    resolve_reasoning_effort,
)
from cco_eval.pages import markdown_report, public_run_config, publish as write_pages, publish_site
from cco_eval.utils import write_json
from cco_eval.waterloo import Budget, GIVE_UP_TIMEOUT_S, Lane, Mode, RunManifest, Verdict, to_json
from runners.agent_loop import run_agent
from runners.common import (
    DEFAULT_JUDGE_PATH, DEFAULT_ZIPS_ROOT, REPO_ROOT,
    attempt_dir, load_problem, model_slug,
)
from runners.judge_client import DEFAULT_SSH_HOST, REMOTE_ROOT, RemoteJudge
from runners.selection import cluster_select, generate_cluster_inputs, programs_from_results
from runners.single_shot import batch_request, run_one

BENCH = REPO_ROOT


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def read_json(path):
    return json.loads(path.read_text())


def admit(args):
    remote = f"{REMOTE_ROOT}/src-cli"
    command(["ssh", args.ssh_host, f"mkdir -p {remote}"])
    command(["rsync", "-aR", "--exclude=.git", "--exclude=.venv", "--exclude=__pycache__", "code/cco-eval",
             "statements", "env-study", "problems", f"{args.ssh_host}:{remote}/"], cwd=REPO_ROOT)
    argv = ["python3", "code/cco-eval/admit.py", "--zips-root", args.zips_root,
            "--corpus", "statements", "--requirements",
            "env-study/problem-requirements.jsonl",
            "--out", "problems", "--checked-at", "2026-10-20T00:00:00Z"]
    command(["ssh", args.ssh_host, f"cd {remote} && {shlex.join(argv)}"])
    command(["rsync", "-a", f"{args.ssh_host}:{remote}/problems/",
             str(BENCH / "problems") + "/"])
    rows = _records(BENCH / "problems")
    (BENCH / "reports/DASHBOARD.md").write_text(render_dashboard(rows))
    print(metric_line(rows, _main_admitted_count()))
    return 0


def cohort(args):
    value = draw(BENCH / "problems", args.seed, args.deterministic, args.unusual)
    write_json(args.out, value)
    print(json.dumps(value, sort_keys=True))
    return 0


def telemetry(result):
    generation = result["attempt"]["generation"]
    calls = generation.get("calls")
    attempt = result["attempt"]
    meta = generation.get("raw_provider_metadata", {})
    usage = meta.get("usage", {})
    row = {
        "attempt_id": attempt["id"], "problem_id": attempt["problem_id"], "model": attempt["model"],
        "language": attempt["language"], "input_tokens": generation.get("input_tokens"),
        "output_tokens": generation.get("output_tokens"),
        "reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens"),
        "cost_usd": usage.get("cost"), "latency_ms": generation.get("latency_ms"),
        "finish_reason": generation.get("stop_reason"), "upstream_provider": meta.get("provider"),
        "extraction_error": generation.get("extraction_error"),
    }
    if calls is None:
        return row
    rows = [telemetry({"attempt": {**attempt, "generation": call}}) for call in calls]
    for field in ("input_tokens", "output_tokens", "reasoning_tokens", "cost_usd", "latency_ms"):
        values = [r[field] for r in rows]
        row[field] = sum(values) if values and all(v is not None for v in values) else None
    row["turns_used"] = result["attempt"].get("turns_used")
    row["agent_outcome"] = result["attempt"].get("agent_outcome")
    row["wall_s"] = result["attempt"].get("wall_s")
    row["finish_reason"] = rows[-1]["finish_reason"] if rows else None
    row["upstream_provider"] = rows[-1]["upstream_provider"] if rows else None
    return row


def admitted_problem_ids(problems_root):
    return {p.parent.name for p in problems_root.glob("*/problem.json")
            if (p.with_name("status.json").is_file() and read_json(p.with_name("status.json"))["status"] == "admitted")}


def _catalog_or_empty():
    try:
        return load_openrouter_catalog()
    except Exception as exc:
        print(f"waterloo: OpenRouter catalog unavailable ({exc}); using the reasoning token floor",
              file=sys.stderr)
        return {}


def batch_custom_id(problem_id, sample_index):
    return f"{problem_id}--s{sample_index}"


def _finished(artifact):
    return (artifact / "attempt.json").exists() and (artifact / "verdict.json").exists()


def _batch_worst_case(model, requests, catalog):
    return sum(batch_api.worst_case_cost(model, catalog, batch_api.estimate_prompt_tokens(messages),
                                         int(req.body["max_tokens"]))
               for req, messages in requests)


def run_batch_model(args, model, problems, run_dir, catalog, *, http=None, sleep=None):
    """Submit (or resume) one Batch API job for every unfinished sample of ``model``.

    Returns (custom_id -> GenerateResult or error string, state path, state), or None when
    nothing is pending. The job id lives in
    ``batches/<model>.active.json`` until every answered sample is recorded."""
    pending = [(p, i) for p in problems for i in range(1, args.k + 1)
               if not _finished(attempt_dir(args.out, run_dir.name, p.id, args.mode, model, i))]
    state_path = run_dir / "batches" / f"{model_slug(model)}.active.json"
    if not pending and not state_path.exists():
        return None
    http = http or batch_api.OpenRouterHTTP(timeout=args.timeout)
    requests = [batch_request(p, model, batch_custom_id(p.id, i), max_tokens=args.max_tokens,
                              temperature=args.temperature, reasoning_effort=args.reasoning_effort,
                              lane=Lane(args.lane), catalog=catalog) for p, i in pending]
    if not state_path.exists():
        worst = _batch_worst_case(model, requests, catalog)
        remaining = batch_api.ensure_affordable(http, worst)
        print(f"batch guard model={model} worst_case_usd={worst:.4f} credits_usd={remaining:.4f}", flush=True)
    state = batch_api.submit_or_resume(model, [r for r, _ in requests], state_path, http)
    kwargs = {"sleep": sleep} if sleep else {}
    final = batch_api.poll(state, state_path, http, **kwargs)
    return batch_api.map_results(final, state, http), state_path, state


def run(args):
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.batch:
        models = [batch_api.as_batch_model(m) for m in models]
    if not models or len(set(models)) != len(models) or any(not m.startswith("openrouter/") for m in models):
        raise ValueError("--models must contain unique openrouter/... IDs")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.k < 1:
        raise ValueError("--k must be positive")
    if args.budget is not None:
        raise ValueError("wall-clock --budget was removed; use --max-turns (HTTP timeout is --timeout)")
    if args.mode == "single_shot" and args.multi_submit:
        raise ValueError("--multi-submit applies only to agentic mode")
    if args.cluster_select and args.k < 2:
        raise ValueError("--cluster-select is only meaningful when --k is greater than 1")
    if args.temperature < 0:
        raise ValueError("--temperature must be non-negative")
    batch_models = [m for m in models if batch_api.is_batch_model(m)]
    if batch_models and args.mode != "single_shot":
        raise ValueError("Batch API (:batch models or --batch) works only in single_shot mode; "
                         "agentic turns depend on tool results and cannot be batched")
    if batch_models and args.cluster_select:
        raise ValueError("--cluster-select is not supported with Batch API models")
    lane = Lane(args.lane)
    budget = Budget(max_turns=args.max_turns) if args.mode == "agentic" else None
    if budget is not None and budget.max_turns <= 0:
        raise ValueError("--max-turns must be positive")
    selection = read_json(args.cohort)
    if not isinstance(selection, dict) or not isinstance(selection.get("deterministic"), list) or not isinstance(selection.get("unusual", []), list):
        raise ValueError("cohort must have deterministic and optional unusual ID lists")
    ids = selection["deterministic"] + selection.get("unusual", [])
    if not ids or len(set(ids)) != len(ids) or not set(ids) <= admitted_problem_ids(BENCH / "problems"):
        raise ValueError("cohort must contain unique admitted problems; draw with --unusual 0")
    problems = [load_problem(pid) for pid in ids]
    if any(not p.statement or not p.tests_path for p in problems):
        raise ValueError("cohort has missing statements or tests")
    catalog = _catalog_or_empty()
    judge = RemoteJudge(ssh_host=args.ssh_host, judge_path=args.judge_path,
                        zips_root=args.zips_root, backend="vm", ssh_timeout=3600)
    env_id = "dry-run" if args.dry_run else judge.env_id()
    sha = command(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True).stdout.strip()
    max_tokens = {m: resolve_max_tokens(m, args.max_tokens, catalog=catalog) for m in models}
    effort_sent = {m: resolve_reasoning_effort(m, args.reasoning_effort, catalog) for m in models}
    if batch_models and catalog and any(batch_api.model_id(m) not in catalog for m in batch_models):
        missing = [m for m in batch_models if batch_api.model_id(m) not in catalog]
        raise ValueError(f"no OpenRouter batch variant for {', '.join(missing)}")
    config = dict(
        git_sha=sha, env_id=env_id,
        cohort_hash=hashlib.sha256(args.cohort.read_bytes()).hexdigest(), models=models,
        mode=Mode(args.mode),
        caps={"max_tokens": max_tokens, "reasoning_effort": effort_sent,
              "backend": "vm", "memory_mb": 2048, "give_up_timeout_s": GIVE_UP_TIMEOUT_S,
              **({"batch_models": batch_models} if batch_models else {})},
        budget=budget, problem_ids=ids, timeout_s=args.timeout,
        judge={"ssh_host": args.ssh_host, "judge_path": args.judge_path, "zips_root": args.zips_root},
        k=args.k, temperature=args.temperature, reasoning_effort=args.reasoning_effort,
        lane=lane.value, selection="cluster" if args.cluster_select else "pass_at_k",
        prompt_version=PROMPT_VERSION, multi_submit=args.multi_submit,
    )
    identity = hashlib.sha256(json.dumps(to_json(RunManifest(run_id="", **config)), sort_keys=True).encode()).hexdigest()[:16]
    run_id = args.run_id or f"waterloo-{identity}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id) or run_id in {".", ".."}:
        raise ValueError("invalid run ID")
    run_dir = args.out / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = RunManifest(run_id=run_id, **config)
    manifest_path = run_dir / "run.json"
    if manifest_path.exists() and read_json(manifest_path) != to_json(manifest):
        raise ValueError("run manifest differs; use a different --run-id (including after changing git SHA)")
    write_json(manifest_path, to_json(manifest))
    write_json(run_dir / "run-config.json", public_run_config(to_json(manifest)))
    print(f"run_id={run_id}", flush=True)
    if args.dry_run:
        print(json.dumps(public_run_config(to_json(manifest)), indent=2, sort_keys=True))
        for model in batch_models:
            requests = [batch_request(p, model, batch_custom_id(p.id, i), max_tokens=args.max_tokens,
                                      temperature=args.temperature, reasoning_effort=args.reasoning_effort,
                                      lane=lane, catalog=catalog)
                        for p in problems for i in range(1, args.k + 1)]
            print(f"batch dry-run model={model} submit_as={batch_api.submit_slug(model)} "
                  f"requests={len(requests)} worst_case_usd={_batch_worst_case(model, requests, catalog):.4f}",
                  flush=True)
        return 0
    completed = []
    batch_failures = []
    for model in models:
        batch_results, batch_state_path, batch_state = {}, None, None
        if model in batch_models:
            if not os.getenv("OPENROUTER_API_KEY"):
                raise ValueError("missing OPENROUTER_API_KEY")
            outcome = run_batch_model(args, model, problems, run_dir, catalog)
            if outcome:
                batch_results, batch_state_path, batch_state = outcome
        for problem in problems:
            problem_results = []
            for sample_index in range(1, args.k + 1):
                artifact = attempt_dir(args.out, run_id, problem.id, args.mode, model, sample_index)
                if _finished(artifact):
                    attempt, verdict = read_json(artifact / "attempt.json"), read_json(artifact / "verdict.json")
                    if (attempt["id"] != verdict["attempt_id"] or attempt["env_id"] != env_id
                        or verdict["env_id"] != env_id or verdict["problem_id"] != problem.id
                        or attempt["problem_id"] != problem.id or attempt["model"] != model
                        or attempt["mode"] != args.mode or attempt["sample_index"] != sample_index):
                        raise ValueError(f"invalid finished attempt: {artifact}")
                    result = {"attempt": attempt, "verdict": verdict, "artifact": str(artifact)}
                    print(f"skip finished model={model} problem={problem.id} sample={sample_index}", flush=True)
                else:
                    if not os.getenv("OPENROUTER_API_KEY"):
                        raise ValueError("missing OPENROUTER_API_KEY")
                    batch_generation = None
                    if model in batch_models:
                        batch_generation = batch_results.get(batch_custom_id(problem.id, sample_index))
                        if not hasattr(batch_generation, "text"):
                            batch_failures.append(f"{model} {problem.id} s{sample_index}: {batch_generation}")
                            print(f"batch request failed model={model} problem={problem.id} "
                                  f"sample={sample_index}: {batch_generation}", flush=True)
                            continue
                    if budget:
                        result = run_agent(
                            problem, model, sample_index, out=args.out, run_id=run_id, judge=judge,
                            max_turns=budget.max_turns, request_timeout_s=args.timeout,
                            max_tokens=args.max_tokens, temperature=args.temperature,
                            reasoning_effort=args.reasoning_effort, lane=lane, catalog=catalog,
                            multi_submit=args.multi_submit,
                        )
                    else:
                        result = run_one(
                            problem, model, sample_index, out=args.out, run_id=run_id, judge=judge,
                            timeout=args.timeout, max_tokens=args.max_tokens, temperature=args.temperature,
                            reasoning_effort=args.reasoning_effort, lane=lane, catalog=catalog,
                            generation=batch_generation,
                        )
                    if result["attempt"].get("agent_outcome") == "harness_error":
                        raise RuntimeError(f"agent attempt ended in harness_error, not scored: {artifact}; "
                                           "rerun the same command to retry it")
                    raw = result["verdict"]
                    if result["attempt"]["env_id"] != env_id or raw.get("env_id", env_id) != env_id:
                        raise ValueError("judge environment changed during the run; start a new run")
                    verdict = Verdict(attempt_id=result["attempt"]["id"], problem_id=problem.id,
                                      subtask_pass=raw.get("subtask_pass", {}), all_pass=raw["all_pass"],
                                      partial_score=raw.get("partial_score", 0.0), outcome=raw["outcome"], env_id=env_id)
                    write_json(artifact / "verdict.json", to_json(verdict))
                    result["verdict"] = to_json(verdict)
                    print(f"finished model={model} problem={problem.id} sample={sample_index} "
                          f"outcome={verdict.outcome} partial_score={verdict.partial_score}", flush=True)
                problem_results.append(result)
                completed.append(result)
            if args.cluster_select:
                try:
                    extra = generate_cluster_inputs(
                        problem, model, timeout=args.timeout, max_tokens=max_tokens[model],
                        temperature=args.temperature, reasoning_effort=args.reasoning_effort,
                        catalog=catalog,
                    )
                except Exception as exc:
                    print(f"cluster inputs unavailable ({exc}); clustering on official samples", flush=True)
                    extra = []
                decision = cluster_select(
                    programs_from_results(problem_results), problem=problem, judge=judge, extra_inputs=extra,
                )
                model_dir = attempt_dir(args.out, run_id, problem.id, args.mode, model, 1).parent
                write_json(model_dir / "selection.json", decision.to_json())
                print(f"cluster model={model} problem={problem.id} chosen={decision.chosen_index} "
                      f"reason={decision.reason}", flush=True)
        if batch_state_path is not None:
            batch_api.archive(batch_state_path, batch_state)
    (run_dir / "telemetry.jsonl").write_text("".join(json.dumps(telemetry(r), sort_keys=True) + "\n" for r in completed))
    if batch_failures:
        raise RuntimeError(f"{len(batch_failures)} batch request(s) got no usable response and were not scored; "
                           "rerun the same command to resubmit them: " + "; ".join(batch_failures))
    return 0


def report(args):
    run_dir = args.out / args.run_id
    manifest_path = run_dir / "run.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else {
        "run_id": args.run_id, "k": 1, "lane": "", "mode": "", "selection": "pass_at_k",
    }
    text = markdown_report(manifest, run_dir, include_download=False)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def publish(args):
    if len(args.run_id) == 1 and not args.note:
        write_pages(args.out / args.run_id[0], args.site)
    else:
        publish_site([args.out / run_id for run_id in args.run_id], args.site, args.note)
    print(args.site / "index.html")
    return 0


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    admission = commands.add_parser("admit", help="Run admission on the judge host, dashboard, and suite metric")
    admission.set_defaults(handler=admit)
    draw_cmd = commands.add_parser("cohort", help="Draw a seeded cohort")
    draw_cmd.add_argument("--out", type=Path, default=BENCH / "cohorts/cohort.json")
    draw_cmd.add_argument("--seed", type=int, default=20261020)
    draw_cmd.add_argument("--deterministic", type=int, default=10)
    draw_cmd.add_argument("--unusual", type=int, default=4)
    draw_cmd.set_defaults(handler=cohort)
    experiment = commands.add_parser("run", help="Generate and judge")
    experiment.add_argument("--models", required=True)
    experiment.add_argument("--cohort", required=True, type=Path)
    experiment.add_argument("--mode", required=True, choices=[m.value for m in Mode])
    experiment.add_argument("--budget", help=argparse.SUPPRESS)
    experiment.add_argument("--max-turns", type=int, default=20,
                            help="Agentic stopping condition (model turns). There is no wall-clock budget.")
    experiment.add_argument("--timeout", type=float, default=DEFAULT_HTTP_TIMEOUT_S,
                            help="HTTP transport timeout in seconds for dead connections (retried). "
                                 "Not a generation budget. Default 7200.")
    experiment.add_argument("--max-tokens", type=int, help="Override catalog/floor output-token cap.")
    experiment.add_argument("--k", type=int, default=1, help="Samples per problem. Default 1.")
    experiment.add_argument("--temperature", type=float, default=0.0)
    experiment.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                            choices=["high", "medium", "low", "none"],
                            help="Reasoning effort sent to the provider. Default high.")
    experiment.add_argument("--lane", default=Lane.CPP.value, choices=[lane.value for lane in Lane],
                            help="Language lane: cpp (default, headline), python, or free.")
    experiment.add_argument("--cluster-select", action="store_true",
                            help="AlphaCode-style filter/cluster selection. Off by default; requires --k > 1.")
    experiment.add_argument("--multi-submit", action="store_true",
                            help="Agentic: allow multiple submits with verdict-only feedback. Off by default.")
    experiment.add_argument("--batch", action="store_true",
                            help="Single-shot: send each model through OpenRouter's Batch API (same as "
                                 "naming it <model>:batch). Async, batch-priced; polled with no wall clock.")
    experiment.add_argument("--dry-run", action="store_true",
                            help="Resolve and write run-config without calling a model or the judge.")
    experiment.add_argument("--run-id")
    experiment.set_defaults(handler=run)
    summary = commands.add_parser("report", help="Summarize a run without contacting APIs")
    summary.add_argument("run_id")
    summary.set_defaults(handler=report)
    pages = commands.add_parser("publish", help="Write a concise GitHub Pages report (not for committing to main)")
    pages.add_argument("run_id", nargs="+", help="One run, or several for an overview page.")
    pages.add_argument("--note", action="append", default=[], help="Overview footnote (repeatable).")
    pages.add_argument("--site", type=Path, default=BENCH / "site")
    pages.set_defaults(handler=publish)
    for sub in (admission, experiment):
        sub.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
        sub.add_argument("--zips-root", default=DEFAULT_ZIPS_ROOT)
    experiment.add_argument("--judge-path", default=DEFAULT_JUDGE_PATH)
    for sub in (experiment, summary, pages):
        sub.add_argument("--out", type=Path, default=BENCH / "runs-raw")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"waterloo: {exc}", file=sys.stderr)
        return 1

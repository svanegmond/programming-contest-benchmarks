"""CCO adapter commands: index, list, run."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
from pathlib import Path

from cco_eval.code_runner import compile_solution, discover_tests, extract_code, run_tests
from cco_eval.corpus_cco import (
    DEFAULT_INDEX_PATH,
    build_index,
    build_prompt,
    find_problem,
    iter_problems,
)
from cco_eval.models import GenerateResult
from cco_eval.providers import (
    dry_run_response,
    generate_text,
    generation_stage,
    mark_generation_span,
)
from cco_eval.tracing import JsonlTracer
from cco_eval.utils import append_jsonl, sha256_text, utc_now, write_json


def command_index(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus).resolve()
    index = build_index(corpus)
    index_path = Path(args.index_path).resolve() if args.index_path else DEFAULT_INDEX_PATH
    write_json(index_path, index)
    print(f"Indexed {index['summary']['years']} years and {index['summary']['problems']} problems")
    print(f"Wrote {index_path}")
    for year_entry in index["years"]:
        print(f"{year_entry['year']}: {len(year_entry['problems'])} problems")
    return 0


def command_list(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus).resolve()
    index = build_index(corpus)
    if args.json:
        rows = [
            {
                "id": problem.get("id"),
                "year": problem.get("year"),
                "day": problem.get("day"),
                "day_problem": problem.get("day_problem"),
                "title": problem.get("title"),
            }
            for problem in iter_problems(index)
        ]
        print(json.dumps(rows, indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    for problem in iter_problems(index):
        title = problem.get("title") or ""
        print(
            f"{problem.get('id')}\tDay {problem.get('day')}, "
            f"Problem {problem.get('day_problem')}\t{title}"
        )
    return 0


def command_run(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "trace.jsonl"
    tracer = JsonlTracer(trace_path)
    stages: dict[str, dict[str, object]] = {}
    score: dict[str, object] = {
        "compile_success": False,
        "sample_passed": None,
        "official_tests_passed": 0,
        "official_tests_total": 0,
        "pass_rate": None,
    }

    with tracer.span("index_corpus", {"corpus": str(corpus), "problem_id": args.problem}) as span:
        try:
            index = build_index(corpus)
            span.set_attribute("problem_count", index["summary"]["problems"])
            stages["index"] = {"ok": True}
        except Exception as exc:
            span.set_error(str(exc), exc)
            stages["index"] = {"ok": False, "reason": str(exc)}
            raise

    with tracer.span("load_problem", {"problem_id": args.problem}) as span:
        try:
            problem = find_problem(index, args.problem)
            span.set_attribute("title", problem.get("title"))
            stages["load_problem"] = {"ok": True}
        except Exception as exc:
            span.set_error(str(exc), exc)
            stages["load_problem"] = {"ok": False, "reason": str(exc)}
            raise

    with tracer.span("build_prompt", {"problem_id": args.problem}) as span:
        prompt = build_prompt(problem)
        span.set_attribute("prompt_sha256", sha256_text(prompt))
        span.set_attribute("prompt_chars", len(prompt))
        stages["build_prompt"] = {"ok": True}

    provider = getattr(args, "provider", "ollama")
    generation = GenerateResult(text="", provider=provider, model=args.model, error="generation_not_started")
    with tracer.span("model_generate", {"provider": provider, "model": args.model, "dry_run": args.dry_run}) as span:
        try:
            generation = generate_text(
                provider,
                args.model,
                prompt,
                dry_run=args.dry_run,
                dry_response=dry_run_response(),
                mock_response=args.mock_response,
                ollama_url=args.ollama_url,
                ollama_timeout=args.ollama_timeout,
                anthropic_api_key=args.anthropic_api_key,
                anthropic_timeout=args.anthropic_timeout,
                max_tokens=args.max_tokens,
            )
            raw_response = generation.text
            mark_generation_span(span, generation)
            stages["generate"] = generation_stage(generation)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            span.set_error(str(exc), exc)
            generation = GenerateResult(
                text="",
                provider=provider,
                model=args.model,
                error=str(exc),
            )
            raw_response = ""
            stages["generate"] = generation_stage(generation)

    with tracer.span("extract_code", {"problem_id": args.problem}) as span:
        code = extract_code(raw_response)
        span.set_attribute("solution_sha256", sha256_text(code))
        span.set_attribute("solution_chars", len(code))
        stages["extract_code"] = {"ok": bool(code.strip())}
        if not code.strip():
            stages["extract_code"]["reason"] = "empty_solution"

    prompt_path = out_dir / "prompt.txt"
    raw_response_path = out_dir / "raw_response.txt"
    solution_path = out_dir / "solution.py"
    prompt_path.write_text(prompt, encoding="utf-8")
    raw_response_path.write_text(raw_response, encoding="utf-8")
    solution_path.write_text(code, encoding="utf-8")

    with tracer.span("compile_solution", {"solution": str(solution_path)}) as span:
        compile_result = compile_solution(solution_path, args.compile_timeout)
        span.set_attribute("returncode", compile_result.get("returncode"))
        if not compile_result.get("ok"):
            span.set_error(str(compile_result.get("reason") or compile_result.get("stderr") or "compile_failed"))
        stages["compile"] = {
            "ok": bool(compile_result.get("ok")),
            "returncode": compile_result.get("returncode"),
        }
        if not compile_result.get("ok"):
            stages["compile"]["reason"] = compile_result.get("reason") or "compile_failed"
        score["compile_success"] = bool(compile_result.get("ok"))

    with tracer.span("discover_tests", {"problem_id": args.problem}) as span:
        discovery = discover_tests(corpus, problem)
        span.set_attribute("ok", discovery.get("ok"))
        span.set_attribute("reason", discovery.get("reason"))
        span.set_attribute("case_count", len(discovery.get("cases", [])))
        stages["discover_tests"] = {
            "ok": bool(discovery.get("ok")),
            "reason": discovery.get("reason"),
            "cases": len(discovery.get("cases", [])),
        }

    with tracer.span("run_tests", {"problem_id": args.problem, "max_official_tests": args.max_official_tests}) as span:
        if compile_result.get("ok"):
            test_result = run_tests(
                discovery,
                solution_path,
                args.test_timeout,
                args.max_official_tests,
                args.max_input_bytes,
                args.max_output_bytes,
            )
        else:
            test_result = {
                "ok": False,
                "reason": "compile_failed",
                "sample_passed": None,
                "official_tests_passed": 0,
                "official_tests_total": 0,
                "official_tests_discovered": int(discovery.get("official_count", 0) or 0),
                "official_tests_skipped": int(discovery.get("official_count", 0) or 0),
                "results": [],
            }
        span.set_attribute("ok", test_result.get("ok"))
        span.set_attribute("reason", test_result.get("reason"))
        span.set_attribute("sample_passed", test_result.get("sample_passed"))
        span.set_attribute("official_tests_passed", test_result.get("official_tests_passed"))
        span.set_attribute("official_tests_total", test_result.get("official_tests_total"))
        if not test_result.get("ok"):
            span.set_error(str(test_result.get("reason") or "test_failed"))
        stages["test"] = {
            "ok": bool(test_result.get("ok")),
            "reason": test_result.get("reason"),
        }
        score.update(
            {
                "sample_passed": test_result.get("sample_passed"),
                "official_tests_passed": test_result.get("official_tests_passed", 0),
                "official_tests_total": test_result.get("official_tests_total", 0),
                "official_tests_discovered": test_result.get("official_tests_discovered", 0),
                "official_tests_skipped": test_result.get("official_tests_skipped", 0),
                "pass_rate": (
                    test_result.get("official_tests_passed", 0) / test_result.get("official_tests_total", 0)
                    if test_result.get("official_tests_total", 0)
                    else None
                ),
            }
        )

    run_id_source = "|".join(
        [
            str(args.problem),
            str(args.model),
            sha256_text(prompt),
            sha256_text(raw_response),
            sha256_text(code),
        ]
    )
    run_id = hashlib.sha256(run_id_source.encode("utf-8")).hexdigest()[:20]
    result_record = {
        "run_id": run_id,
        "timestamp": utc_now(),
        "adapter": "cco",
        "benchmark": "cco_programming",
        "item_id": args.problem,
        "problem_id": args.problem,
        "provider": provider,
        "model": args.model,
        "generation": generation.to_record(),
        "language": "python",
        "stages": stages,
        "score": score,
        "artifacts": {
            "prompt": prompt_path.name,
            "raw_response": raw_response_path.name,
            "solution": solution_path.name,
            "trace": trace_path.name,
            "test_results": "test_results.json",
            "metadata": "metadata.json",
        },
    }

    with tracer.span("write_results", {"run_id": run_id}) as span:
        write_json(out_dir / "test_results.json", test_result)
        write_json(
            out_dir / "metadata.json",
            {
                "problem": {
                    "id": problem.get("id"),
                    "year": problem.get("year"),
                    "day": problem.get("day"),
                    "day_problem": problem.get("day_problem"),
                    "title": problem.get("title"),
                    "raw_html_path": problem.get("raw_html_path"),
                    "test_data_zip": problem.get("test_data_zip"),
                },
                "compile": compile_result,
                "test_discovery": {
                    "ok": discovery.get("ok"),
                    "reason": discovery.get("reason"),
                    "zip_path": discovery.get("zip_path"),
                    "case_count": len(discovery.get("cases", [])),
                    "sample_count": discovery.get("sample_count"),
                    "official_count": discovery.get("official_count"),
                },
                "runner": {
                    "provider": provider,
                    "ollama_url": args.ollama_url if provider == "ollama" else None,
                    "dry_run": args.dry_run,
                    "max_official_tests": args.max_official_tests,
                    "max_input_bytes": args.max_input_bytes,
                    "max_output_bytes": args.max_output_bytes,
                },
                "generation": generation.to_record(),
            },
        )
        append_jsonl(out_dir / "results.jsonl", result_record)
        span.set_attribute("results_path", str(out_dir / "results.jsonl"))

    print(f"Wrote {out_dir / 'results.jsonl'}")
    print(f"Wrote {trace_path}")
    return 0 if stages.get("generate", {}).get("ok") else 2

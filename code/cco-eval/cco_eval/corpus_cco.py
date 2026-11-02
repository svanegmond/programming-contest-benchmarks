"""CCO problem corpus: index building, problem lookup, prompt construction."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cco_eval import PROJECT_DIR
from cco_eval.html_parsing import cleanup_statement, html_to_text
from cco_eval.tracing import JsonlTracer
from cco_eval.utils import compact_ws, utc_now

DEFAULT_INDEX_PATH = PROJECT_DIR / "cco_eval_index.json"

DEFAULT_SMOKE_CCO_ITEMS = ["2018-p1", "2023-p1", "2024-p1"]


def parse_problem_html(year: str, html_path: Path) -> tuple[list[dict[str, object]], list[str]]:
    limitations: list[str] = []
    try:
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"could_not_read_html: {exc}"]

    text = html_to_text(html_text)
    matches = list(re.finditer(r"\[\[H2\]\]\s*(.*?)\s*\[\[/H2\]\]", text, re.DOTALL))
    problems: list[dict[str, object]] = []
    sequence = 0
    heading_re = re.compile(r"^Day\s+(\d+)\s*,\s*Problem\s+(\d+)\s*:?\s*(.*)$", re.IGNORECASE)

    for idx, match in enumerate(matches):
        heading = compact_ws(match.group(1))
        heading_match = heading_re.match(heading)
        if not heading_match:
            continue
        sequence += 1
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        statement = cleanup_statement(text[start:end])
        title = compact_ws(heading_match.group(3))
        problem = {
            "id": f"{year}-p{sequence}",
            "year": year,
            "sequence": sequence,
            "day": int(heading_match.group(1)),
            "day_problem": int(heading_match.group(2)),
            "title": title or None,
            "heading": heading,
            "statement": statement,
            "raw_html_path": str(html_path),
        }
        problems.append(problem)

    if not problems:
        limitations.append("no_h2_day_problem_headings_found")
    elif len(problems) < 6:
        limitations.append(f"expected_6_problems_found_{len(problems)}")
    return problems, limitations


def load_manifest(corpus: Path) -> dict[str, object]:
    manifest_path = corpus / "manifest.json"
    with manifest_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_index(corpus: Path, tracer: JsonlTracer | None = None) -> dict[str, object]:
    tracer = tracer or JsonlTracer(None)
    with tracer.span("index_corpus", {"corpus": str(corpus)}):
        manifest = load_manifest(corpus)
        by_year: dict[str, dict[str, dict[str, object]]] = {}
        for resource in manifest.get("resources", []):
            if not isinstance(resource, dict) or "year" not in resource:
                continue
            year = str(resource["year"])
            kind = str(resource.get("kind", ""))
            if kind:
                by_year.setdefault(year, {})[kind] = resource

        years: list[dict[str, object]] = []
        for year in sorted(by_year.keys(), reverse=True):
            resources = by_year[year]
            year_entry: dict[str, object] = {
                "year": year,
                "resources": {
                    kind: resources[kind].get("path")
                    for kind in sorted(resources.keys())
                    if resources[kind].get("path")
                },
                "problems": [],
                "parser_limitations": [],
            }
            contest_html = resources.get("contest_html", {}).get("path")
            if contest_html:
                html_path = corpus / str(contest_html)
                problems, limitations = parse_problem_html(year, html_path)
                for problem in problems:
                    problem["raw_html_path"] = str(contest_html)
                    problem["contest_pdf"] = resources.get("contest_pdf", {}).get("path")
                    problem["test_data_zip"] = resources.get("test_data_zip", {}).get("path")
                    problem["results_pdf"] = resources.get("results_pdf", {}).get("path")
                year_entry["problems"] = problems
                year_entry["parser_limitations"] = limitations
            else:
                year_entry["parser_limitations"] = ["missing_contest_html"]
            years.append(year_entry)

        total_problems = sum(len(year_entry["problems"]) for year_entry in years)
        return {
            "schema_version": 1,
            "generated_at": utc_now(),
            "corpus": str(corpus.resolve()),
            "manifest_path": str((corpus / "manifest.json").resolve()),
            "years": years,
            "summary": {"years": len(years), "problems": total_problems},
        }


def iter_problems(index: dict[str, object]):
    for year_entry in index.get("years", []):
        if not isinstance(year_entry, dict):
            continue
        for problem in year_entry.get("problems", []):
            if isinstance(problem, dict):
                yield problem


def find_problem(index: dict[str, object], problem_id: str) -> dict[str, object]:
    for problem in iter_problems(index):
        if problem.get("id") == problem_id:
            return problem
    raise KeyError(f"problem not found: {problem_id}")


def select_cco_problem_ids(index: dict[str, object], requested: list[str], count: int | None) -> list[str]:
    ids = requested or list(DEFAULT_SMOKE_CCO_ITEMS)
    if count is not None:
        ids = ids[: max(0, count)]
    for problem_id in ids:
        find_problem(index, problem_id)
    return ids


def build_prompt(problem: dict[str, object]) -> str:
    title = problem.get("title") or problem.get("heading") or problem.get("id")
    return f"""You are solving a Canadian Computing Olympiad programming problem.

Derive a full-constraints algorithm silently before writing code. Avoid brute force unless the constraints make it safe.
Return one complete Python 3 program in a Python fenced code block.
Use stdin for input and stdout for output. Do not include explanation outside the code block.

Problem ID: {problem.get("id")}
Problem title: {title}

Problem statement:
{problem.get("statement", "")}
"""

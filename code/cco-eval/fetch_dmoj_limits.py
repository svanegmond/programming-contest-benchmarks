#!/usr/bin/env python3
"""Look up DMOJ memory limits for Waterloo problems whose statements omit one.

Problems are matched by title within the contest year, never by problem number:
DMOJ's ccoYYpN numbering differs from the statements for 2016-2019.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

API = "https://dmoj.ca/api/v2"
SITE = "https://dmoj.ca/problem"
MAIN_CODE = re.compile(r"^cco(\d\d)p\d+$")

# DMOJ renamed these problems; each pairing was checked by hand against the statement text
# (same year, same time limit, and every other problem of that year already matched by title).
TITLE_ALIASES = {"cco-2017-d2p1": "cco17p4"}

Fetch = Callable[[str], dict[str, Any]]


def http_get_json(url: str, retries: int = 6) -> dict[str, Any]:
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "cco-eval"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(3 * 2 ** attempt)
    raise AssertionError("unreachable")


def normalize_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.casefold()))


def dmoj_title(name: str) -> str:
    return name.split(" - ", 1)[1] if " - " in name else name


def list_cco_problems(fetch: Fetch) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        data = fetch(f"{API}/problems?page={page}")["data"]
        found.extend(item for item in data["objects"] if MAIN_CODE.match(item["code"]))
        if not data["has_more"]:
            return found
        page += 1


def memory_mb(memory_limit_kb: int) -> int:
    return round(memory_limit_kb / 1024)


def lookup(problem: dict[str, Any], catalog: list[dict[str, Any]], fetch: Fetch) -> dict[str, Any]:
    result: dict[str, Any] = {
        "problem_id": problem["id"],
        "title": problem["title"],
        "dmoj_code": None,
        "dmoj_name": None,
        "memory_limit_mb": None,
        "url": None,
        "match": None,
    }
    year_suffix = f"{problem['year'] % 100:02d}"
    wanted = normalize_title(problem["title"])
    matches = [
        item
        for item in catalog
        if MAIN_CODE.match(item["code"]).group(1) == year_suffix
        and normalize_title(dmoj_title(item["name"])) == wanted
    ]
    alias = TITLE_ALIASES.get(problem["id"])
    if alias is not None:
        matches = [item for item in catalog if item["code"] == alias]
    if len(matches) != 1:
        year_titles = sorted(
            dmoj_title(item["name"]) for item in catalog if MAIN_CODE.match(item["code"]).group(1) == year_suffix
        )
        result["why"] = (
            f"{len(matches)} DMOJ problems titled this in cco{year_suffix}; DMOJ titles that year: {year_titles}"
        )
        return result
    code = matches[0]["code"]
    detail = fetch(f"{API}/problem/{code}")["data"]["object"]
    result.update(
        dmoj_code=code,
        dmoj_name=detail["name"],
        memory_limit_mb=memory_mb(detail["memory_limit"]),
        url=f"{SITE}/{code}",
        match="alias" if alias else "title",
    )
    return result


def load_missing(problems_root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(problems_root.glob("*/problem.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("memory_limit_mb") is None:
            rows.append(row)
    return rows


def build(problems_root: Path, fetch: Fetch = http_get_json) -> list[dict[str, Any]]:
    missing = load_missing(problems_root)
    catalog = list_cco_problems(fetch)
    return [lookup(row, catalog, fetch) for row in missing]


def merge_entries(previous: list[dict[str, Any]], fetched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Problems that already have a limit drop out of `build`; keep their earlier entries."""
    merged = {entry["problem_id"]: entry for entry in previous}
    merged.update((entry["problem_id"], entry) for entry in fetched)
    return [merged[key] for key in sorted(merged)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--problems-root", type=Path, default=root / "problems")
    parser.add_argument("--out", type=Path, default=root / "env-study/dmoj-memory-limits.json")
    args = parser.parse_args()
    entries = build(args.problems_root)
    if args.out.is_file():
        entries = merge_entries(json.loads(args.out.read_text(encoding="utf-8"))["problems"], entries)
    args.out.write_text(
        json.dumps(
            {"source": "https://dmoj.ca/api/v2", "problems": entries}, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    matched = sum(1 for entry in entries if entry["dmoj_code"])
    print(f"matched {matched}/{len(entries)}")
    for entry in entries:
        if not entry["dmoj_code"]:
            print(f"unmatched {entry['problem_id']} {entry['title']}: {entry['why']}")


if __name__ == "__main__":
    main()

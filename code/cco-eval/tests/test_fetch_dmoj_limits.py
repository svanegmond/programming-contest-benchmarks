import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetch_dmoj_limits import lookup, merge_entries, normalize_title

CATALOG = [
    {"code": "cco19p1", "name": "CCO '19 P1 - Human Error"},
    {"code": "cco19p2", "name": "CCO '19 P2 - Sirtet"},
    {"code": "cco18p1", "name": "CCO '18 P1 - Sirtet"},
]
DETAILS = {
    "cco19p1": {"name": "CCO '19 P1 - Human Error", "memory_limit": 262144},
    "cco19p2": {"name": "CCO '19 P2 - Sirtet", "memory_limit": 1048576},
}


def fetch(url: str) -> dict:
    return {"data": {"object": DETAILS[url.rsplit("/", 1)[1]]}}


def problem(pid: str, year: int, title: str) -> dict:
    return {"id": pid, "year": year, "title": title}


def test_match_is_by_title_within_year_not_by_problem_number():
    result = lookup(problem("cco-2019-d1p2", 2019, "Sirtet"), CATALOG, fetch)
    assert result["dmoj_code"] == "cco19p2"
    assert result["memory_limit_mb"] == 1024
    assert result["url"] == "https://dmoj.ca/problem/cco19p2"


def test_memory_limit_converts_kilobytes():
    assert lookup(problem("cco-2019-d1p1", 2019, "Human  Error"), CATALOG, fetch)["memory_limit_mb"] == 256


def test_unmatched_title_records_null_and_why():
    result = lookup(problem("cco-2019-d9p9", 2019, "Nonexistent"), CATALOG, fetch)
    assert result["dmoj_code"] is None
    assert result["memory_limit_mb"] is None
    assert "Sirtet" in result["why"]


def test_normalize_title_ignores_case_and_punctuation():
    assert normalize_title("Geese vs. Hawks") == normalize_title("geese  vs hawks")


def test_merge_keeps_earlier_entries_and_replaces_refetched_ones_in_id_order():
    old = [{"problem_id": "cco-2016-d1p1", "memory_limit_mb": 1024}, {"problem_id": "cco-2015-d1p1", "memory_limit_mb": None}]
    new = [{"problem_id": "cco-2015-d1p1", "memory_limit_mb": 256}]
    assert merge_entries(old, new) == [
        {"problem_id": "cco-2015-d1p1", "memory_limit_mb": 256},
        {"problem_id": "cco-2016-d1p1", "memory_limit_mb": 1024},
    ]

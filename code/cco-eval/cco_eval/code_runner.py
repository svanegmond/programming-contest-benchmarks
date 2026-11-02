"""Code extraction, compilation, test discovery, and test execution."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

from cco_eval.utils import natural_key


def extract_code(raw_response: str) -> str:
    fenced = re.search(r"```(?:python|python3|py)?\s*\n(.*?)```", raw_response, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip() + "\n"
    generic = re.search(r"```\s*\n(.*?)```", raw_response, re.DOTALL)
    if generic:
        return generic.group(1).strip() + "\n"
    return raw_response.strip() + "\n"


def compile_solution(solution_path: Path, timeout: float) -> dict[str, object]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(solution_path)],
            cwd=str(solution_path.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "reason": "compile_timeout",
        }


def discover_tests(corpus: Path, problem: dict[str, object]) -> dict[str, object]:
    zip_rel = problem.get("test_data_zip")
    day = problem.get("day")
    day_problem = problem.get("day_problem")
    if not zip_rel:
        return {"ok": False, "reason": "missing_test_data_zip", "zip_path": None, "cases": []}
    if not isinstance(day, int) or not isinstance(day_problem, int):
        return {"ok": False, "reason": "missing_day_problem_metadata", "zip_path": str(zip_rel), "cases": []}

    zip_path = corpus / str(zip_rel)
    if not zip_path.exists():
        return {"ok": False, "reason": "test_data_zip_not_found", "zip_path": str(zip_path), "cases": []}

    base = f"d{day}p{day_problem}"
    pairs: dict[str, dict[str, zipfile.ZipInfo]] = {}
    title_tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", str(problem.get("title") or "").lower())
        if len(token) >= 4 and token not in {"problem", "with", "from", "that", "this"}
    ]

    def is_modern_match(name: str) -> bool:
        lower = name.lower()
        return f"/{base}." in lower or lower.startswith(f"{base}.")

    def is_legacy_match(name: str) -> bool:
        lower = name.lower()
        day_markers = (f"day{day}/", f"day{day}data/")
        if not any(marker in lower for marker in day_markers):
            return False
        return any(token in lower for token in title_tokens)

    def add_pair(info: zipfile.ZipInfo, ext: str) -> None:
        stem = info.filename.rsplit(".", 1)[0]
        normalized_ext = "out" if ext == "a" else ext
        pairs.setdefault(stem, {})[normalized_ext] = info

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                lower = info.filename.lower()
                if not lower.endswith((".in", ".out", ".a")):
                    continue
                if not is_modern_match(info.filename) and not is_legacy_match(info.filename):
                    continue
                ext = info.filename.rsplit(".", 1)[1].lower()
                add_pair(info, ext)
    except zipfile.BadZipFile as exc:
        return {"ok": False, "reason": f"bad_zip_file: {exc}", "zip_path": str(zip_path), "cases": []}

    cases: list[dict[str, object]] = []
    for stem in sorted(pairs.keys(), key=natural_key):
        pair = pairs[stem]
        if "in" not in pair or "out" not in pair:
            continue
        kind = "sample" if "sample" in stem.lower() else "official"
        cases.append(
            {
                "id": Path(stem).name,
                "kind": kind,
                "input_name": pair["in"].filename,
                "output_name": pair["out"].filename,
                "input_bytes": pair["in"].file_size,
                "output_bytes": pair["out"].file_size,
            }
        )

    if not cases:
        return {"ok": False, "reason": "unsupported_test_layout", "zip_path": str(zip_path), "cases": []}
    return {
        "ok": True,
        "reason": None,
        "zip_path": str(zip_path),
        "cases": cases,
        "sample_count": sum(1 for case in cases if case["kind"] == "sample"),
        "official_count": sum(1 for case in cases if case["kind"] == "official"),
    }


def decode_truncated(data: bytes | str | None, limit: int = 4000) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        text = data
    else:
        text = data[:limit].decode("utf-8", errors="replace")
        if len(data) > limit:
            text += f"\n[truncated {len(data) - limit} bytes]"
    if len(text) > limit:
        return text[:limit] + f"\n[truncated {len(text) - limit} chars]"
    return text


def output_tokens(data: bytes) -> list[bytes]:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split()


def run_one_test(
    zip_path: Path,
    case: dict[str, object],
    solution_path: Path,
    timeout: float,
    max_input_bytes: int,
    max_output_bytes: int,
) -> dict[str, object]:
    if int(case["input_bytes"]) > max_input_bytes:
        return {**case, "status": "skipped", "reason": "input_too_large", "passed": None}
    if int(case["output_bytes"]) > max_output_bytes:
        return {**case, "status": "skipped", "reason": "expected_output_too_large", "passed": None}

    with zipfile.ZipFile(zip_path) as zf:
        input_data = zf.read(str(case["input_name"]))
        expected = zf.read(str(case["output_name"]))

    try:
        proc = subprocess.run(
            [sys.executable, str(solution_path)],
            cwd=str(solution_path.parent),
            input=input_data,
            capture_output=True,
            timeout=timeout,
        )
        passed = proc.returncode == 0 and output_tokens(proc.stdout) == output_tokens(expected)
        return {
            **case,
            "status": "ran",
            "passed": passed,
            "returncode": proc.returncode,
            "stdout": decode_truncated(proc.stdout),
            "stderr": decode_truncated(proc.stderr),
            "expected_preview": decode_truncated(expected),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            **case,
            "status": "timeout",
            "reason": "test_timeout",
            "passed": False,
            "stdout": decode_truncated(exc.stdout),
            "stderr": decode_truncated(exc.stderr),
        }


def run_tests(
    discovery: dict[str, object],
    solution_path: Path,
    timeout: float,
    max_official_tests: int,
    max_input_bytes: int,
    max_output_bytes: int,
) -> dict[str, object]:
    if not discovery.get("ok"):
        return {
            "ok": False,
            "reason": discovery.get("reason", "unsupported_test_layout"),
            "sample_passed": None,
            "official_tests_passed": 0,
            "official_tests_total": 0,
            "official_tests_discovered": 0,
            "official_tests_skipped": 0,
            "results": [],
        }

    zip_path = Path(str(discovery["zip_path"]))
    cases = list(discovery.get("cases", []))
    samples = [case for case in cases if case.get("kind") == "sample"]
    official = [case for case in cases if case.get("kind") == "official"]
    selected_official = official[: max(0, max_official_tests)]
    selected = samples + selected_official
    results = [
        run_one_test(zip_path, case, solution_path, timeout, max_input_bytes, max_output_bytes)
        for case in selected
    ]

    sample_results = [result for result in results if result.get("kind") == "sample" and result.get("status") == "ran"]
    official_results = [result for result in results if result.get("kind") == "official" and result.get("status") == "ran"]
    sample_passed = all(bool(result.get("passed")) for result in sample_results) if sample_results else None
    official_passed = sum(1 for result in official_results if result.get("passed"))
    official_total = len(official_results)
    skipped = sum(1 for result in results if result.get("status") != "ran") + max(0, len(official) - len(selected_official))

    reason = None
    ok = True
    if sample_passed is False:
        ok = False
        reason = "sample_failed"
    elif official_total and official_passed != official_total:
        ok = False
        reason = "official_tests_failed"
    elif not samples and not official:
        ok = False
        reason = "unsupported_test_layout"

    return {
        "ok": ok,
        "reason": reason,
        "sample_passed": sample_passed,
        "official_tests_passed": official_passed,
        "official_tests_total": official_total,
        "official_tests_discovered": len(official),
        "official_tests_skipped": skipped,
        "results": results,
    }

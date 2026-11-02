#!/usr/bin/env python3
"""Admit CCO problems whose statements and official tests can be matched safely."""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cco_eval.corpus_cco import parse_problem_html
from cco_eval.waterloo import (
    Admission,
    AdmissionStatus,
    GradingKind,
    Problem,
    RejectionReason,
    Subtask,
    TestCase,
)


PAIR_EXTENSIONS = {"in": "input", "out": "expected", "a": "expected"}


class SampleHTMLParser(HTMLParser):
    """Keep labeled sample sections and their code blocks in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, str]] = []
        self.heading_tag: str | None = None
        self.heading_parts: list[str] = []
        self.block_tag: str | None = None
        self.block_depth = 0
        self.block_parts: list[str] = []
        self.block_has_code = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"h2", "h3", "h4"} and self.block_tag is None:
            self.heading_tag = tag
            self.heading_parts = []
        if self.heading_tag is not None:
            return
        if self.block_tag is None and tag in {"pre", "p"}:
            self.block_tag = tag
            self.block_depth = 1
            self.block_parts = []
            self.block_has_code = tag == "pre"
        elif self.block_tag is not None:
            self.block_depth += 1
            if tag == "code":
                self.block_has_code = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.heading_tag == tag:
            self.events.append(("heading", " ".join("".join(self.heading_parts).split())))
            self.heading_tag = None
            return
        if self.block_tag is not None:
            self.block_depth -= 1
            if self.block_depth <= 0 or tag == self.block_tag:
                if self.block_has_code:
                    self.events.append(("code", "".join(self.block_parts).strip()))
                self.block_tag = None
                self.block_depth = 0

    def handle_data(self, data: str) -> None:
        if self.heading_tag is not None:
            self.heading_parts.append(data)
        elif self.block_tag is not None:
            self.block_parts.append(data)


def _sample_label(text: str, kind: str) -> bool:
    normalized = " ".join(text.casefold().split())
    if kind == "input":
        return bool(re.fullmatch(r"(?:possible )?sample input(?: \d+)?", normalized))
    return bool(re.fullmatch(r"(?:possible )?output for sample input(?: \d+)?", normalized))


def extract_samples(html_text: str, day: int, day_problem: int) -> list[tuple[str, str]]:
    parser = SampleHTMLParser()
    parser.feed(html_text)
    events = parser.events
    sample_heading = re.compile(
        rf"^Day\s+{day}\s*,\s*Problem\s+{day_problem}\s*:?.*$", re.IGNORECASE
    )
    start = end = None
    for index, (kind, value) in enumerate(events):
        if kind == "heading" and value.startswith("Day ") and sample_heading.match(value):
            start = index
            break
    if start is None:
        return []
    for index in range(start + 1, len(events)):
        kind, value = events[index]
        if kind == "heading" and value.startswith("Day "):
            end = index
            break
    relevant = events[start + 1 : end]
    inputs: list[str] = []
    outputs: list[str] = []
    pending: tuple[str, int] | None = None
    for index, (kind, value) in enumerate(relevant):
        if kind == "heading":
            if _sample_label(value, "input"):
                pending = ("input", len(inputs))
            elif _sample_label(value, "output"):
                pending = ("output", len(outputs))
        elif kind == "code" and pending is not None:
            target, _ = pending
            (inputs if target == "input" else outputs).append(value)
            pending = None
    return list(zip(inputs, outputs))


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def sample_matches(
    samples: list[tuple[str, str]],
    pairs: list[dict[str, Any]],
    zip_path: Path | None = None,
    input_only: bool = False,
) -> str | None:
    """Find a ZIP pair equal to a statement sample.

    `input_only` compares inputs alone: any-valid and scored problems print an
    example output that need not equal the official one; the checker proof
    separately shows the checker accepts the statement's example output.
    """
    sample_set = {(normalize_whitespace(inp), normalize_whitespace(out)) for inp, out in samples}
    sample_inputs = {inp for inp, _ in sample_set}
    if not sample_set:
        return None
    named_samples = [pair for pair in pairs if "sample" in PurePosixPath(pair["input_name"]).name.casefold()]
    max_input_size = max(len(inp.encode("utf-8")) for inp, _ in samples) + 64
    max_output_size = max(len(out.encode("utf-8")) for _, out in samples) + 64
    candidates = named_samples + [
        pair
        for pair in pairs
        if pair not in named_samples
        and pair.get("input_size", max_input_size) <= max_input_size
        and (input_only or pair.get("expected_size", max_output_size) <= max_output_size)
    ]
    archive = zipfile.ZipFile(zip_path) if zip_path is not None and any("input_text" not in p for p in candidates) else None
    try:
        for pair in candidates:
            if input_only:
                if "input_text" in pair:
                    input_text = pair["input_text"]
                elif archive is not None:
                    input_text = archive.read(pair["input_name"]).decode("utf-8", errors="replace")
                else:
                    continue
                if normalize_whitespace(input_text) in sample_inputs:
                    return pair["input_name"]
                continue
            if "input_text" in pair and "expected_text" in pair:
                input_text, expected_text = pair["input_text"], pair["expected_text"]
            elif archive is not None:
                input_text = archive.read(pair["input_name"]).decode("utf-8", errors="replace")
                expected_text = archive.read(pair["expected_name"]).decode("utf-8", errors="replace")
            else:
                continue
            observed = (normalize_whitespace(input_text), normalize_whitespace(expected_text))
            if observed in sample_set:
                return pair["input_name"]
    finally:
        if archive is not None:
            archive.close()
    return None


def problem_key(row: dict[str, Any]) -> tuple[int, int]:
    match = re.search(r"-d(\d+)p(\d+)$", row["id"])
    if not match:
        raise ValueError(f"problem id has no day/problem suffix: {row['id']}")
    return int(match.group(1)), int(match.group(2))


def _title_tokens(title: str) -> tuple[str, ...]:
    return tuple(token for token in re.findall(r"[a-z0-9]+", title.casefold()) if len(token) >= 3)


def _has_modern_problem_path(name: str, row: dict[str, Any]) -> bool:
    day, number = problem_key(row)
    base = re.compile(rf"(?<![a-z0-9])d{day}p{number}(?!\d)", re.IGNORECASE)
    return bool(base.search(PurePosixPath(name).name))


def _has_day_title_path(name: str, row: dict[str, Any]) -> bool:
    day, _ = problem_key(row)
    compact = re.sub(r"[^a-z0-9]", "", name.casefold())
    day_marker = re.search(rf"day{day}(?:data)?", compact) is not None
    title_tokens = tuple(
        token for token in _title_tokens(str(row.get("title", "")))
        if token not in {"and", "the", "with", "from", "into", "over"}
    )

    def occurs(token: str) -> bool:
        variants = {token, token + "s"}
        if token.endswith("y"):
            variants.add(token[:-1] + "ies")
        if token.endswith("e"):
            variants.add(token[:-1] + "es")
        return any(
            re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", name.casefold())
            for variant in variants
        )

    aliases = LAYOUT_TITLE_ALIASES.get(str(row.get("id")), ())
    alias_matches = any(
        re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", name.casefold())
        for alias in aliases
    )
    return day_marker and (any(occurs(token) for token in title_tokens) or alias_matches)


def _has_day_problem_path(name: str, row: dict[str, Any]) -> bool:
    day, number = problem_key(row)
    compact = re.sub(r"[^a-z0-9]", "", name.casefold())
    return (
        re.search(rf"day{day}(?:data)?", compact) is not None
        and re.search(rf"(?:problem|prob|p){number}(?:[^0-9]|$)", compact) is not None
    )


LAYOUT_RECOGNIZERS: tuple[tuple[str, Callable[[str, dict[str, Any]], bool]], ...] = (
    ("dNpM_filename", _has_modern_problem_path),
    ("day_directory_title", _has_day_title_path),
    ("day_directory_problem_number", _has_day_problem_path),
)

LAYOUT_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "cco-2017-d2p2": ("pronet",),
}


def _subtask_marker(name: str) -> int | None:
    match = re.search(r"(?:^|[/_. -])sub[_ -]?task[_ -]?(\d+)(?:$|[/_. -])", name.casefold())
    if match is None:
        match = re.search(r"(?:^|/)s(\d+)(?:/|$)", name.casefold())
    return int(match.group(1)) if match else None


def _weights(row: dict[str, Any]) -> list[Subtask]:
    entries: list[Subtask] = []
    marks = row.get("subtasks", [])
    total = 25.0
    for index, item in enumerate(marks, start=1):
        entries.append(Subtask(name=f"subtask_{index}", weight=float(item["marks"]) / total))
    residual = row.get("implied_full_constraint_marks_inferred")
    if residual:
        entries.append(Subtask(name="full_constraints", weight=float(residual) / total))
    if not entries:
        entries.append(Subtask(name="full_constraints", weight=1.0))
    return entries


def _subtask_for(member: str, row: dict[str, Any]) -> str | None:
    marker = _subtask_marker(member)
    if marker is not None:
        subtasks = _weights(row)
        return subtasks[marker - 1].name if 1 <= marker <= len(subtasks) else None
    day, number = problem_key(row)
    group = re.search(rf"(?:^|/)d{day}p{number}\.(\d+)-", member, re.IGNORECASE)
    if group is None:
        group = re.search(r"[._](\d+)-\d+(?:\.[^.]+)?$", PurePosixPath(member).name)
    if group:
        subtasks = _weights(row)
        index = int(group.group(1))
        return subtasks[index - 1].name if 1 <= index <= len(subtasks) else None
    if "sample" in PurePosixPath(member).name.casefold():
        return _weights(row)[0].name
    compact = re.sub(r"[^a-z0-9]", "", member.casefold())
    if re.search(r"(?:^|/)full(?:constraints?)?(?:/|\.|$)", member.casefold()):
        return "full_constraints" if any(s.name == "full_constraints" for s in _weights(row)) else None
    if "fullconstraints" in compact:
        return "full_constraints" if any(s.name == "full_constraints" for s in _weights(row)) else None
    subtasks = _weights(row)
    return subtasks[0].name if len(subtasks) == 1 else None


def _index_zip(root: Path, year: int) -> Path | None:
    index_path = root / "INDEX.json"
    if not index_path.is_file():
        return None
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    candidates: list[str] = []

    def walk(value: Any, parent_key: str | None = None) -> None:
        if isinstance(value, dict):
            row_year = value.get("year", parent_key)
            if str(row_year) == str(year):
                for key in ("path", "file", "filename", "zip", "zip_path"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.lower().endswith(".zip"):
                        candidates.append(candidate)
            for key, child in value.items():
                walk(child, str(key))
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    for candidate in candidates:
        path = (root / candidate).resolve()
        if path.is_relative_to(root.resolve()) and path.is_file():
            return path
    return None


def locate_zip(zips_root: Path, year: int) -> Path | None:
    indexed = _index_zip(zips_root, year)
    if indexed:
        return indexed
    year_dir = zips_root / str(year)
    if not year_dir.is_dir():
        return None
    candidates = sorted(path for path in year_dir.rglob("*.zip") if path.is_file())
    return candidates[0] if len(candidates) == 1 else None


def _recognized_members(zf: zipfile.ZipFile, row: dict[str, Any]) -> tuple[str | None, list[zipfile.ZipInfo]]:
    files = [info for info in zf.infolist() if not info.is_dir() and info.filename.rsplit(".", 1)[-1].casefold() in PAIR_EXTENSIONS]
    matches_by_recognizer = [
        (name, [info for info in files if recognizer(info.filename, row)])
        for name, recognizer in LAYOUT_RECOGNIZERS
    ]
    matches_by_recognizer = [(name, matches) for name, matches in matches_by_recognizer if matches]
    if not matches_by_recognizer:
        return None, []
    name, matches = matches_by_recognizer[0]
    if name == "dNpM_filename":
        day, number = problem_key(row)
        directory = re.compile(rf"^d{day}p{number}$", re.IGNORECASE)
        matched_directories = {PurePosixPath(info.filename).parent for info in matches}
        matches.extend(
            info
            for info in files
            if "sample" in PurePosixPath(info.filename).name.casefold()
            and (
                PurePosixPath(info.filename).parent in matched_directories
                or any(directory.fullmatch(part) for part in PurePosixPath(info.filename).parts[:-1])
            )
            and info not in matches
        )
    return name, matches


def discover_pairs(
    zip_path: Path,
    zip_relative: str,
    row: dict[str, Any],
    subtasks: list[Subtask],
) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    evidence: list[str] = []
    groups: dict[str, dict[str, zipfile.ZipInfo]] = defaultdict(dict)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            recognizer, members = _recognized_members(zf, row)
            if recognizer is None:
                return None, [], [f"no_recognizer_matched_zip_members:{zip_path.name}"]
            evidence.append(f"layout:{recognizer}")
            for info in members:
                ext = info.filename.rsplit(".", 1)[-1].casefold()
                kind = PAIR_EXTENSIONS[ext]
                normalized_kind = "expected" if kind == "expected" else kind
                stem = info.filename.rsplit(".", 1)[0]
                if normalized_kind in groups[stem]:
                    return None, [], [f"duplicate_{normalized_kind}_member:{stem}"]
                groups[stem][normalized_kind] = info
            pairs: list[dict[str, Any]] = []
            for stem, members_by_kind in sorted(groups.items()):
                if "input" not in members_by_kind or "expected" not in members_by_kind:
                    return recognizer, [], [f"incomplete_test_pair:{stem}"]
                subtask = _subtask_for(stem, row)
                if subtask is None or not any(entry.name == subtask for entry in subtasks):
                    return None, [], [f"subtask_mapping_unclear:{stem}"]
                input_info = members_by_kind["input"]
                output_info = members_by_kind["expected"]
                pairs.append(
                    {
                        "subtask": subtask,
                        "input_name": input_info.filename,
                        "expected_name": output_info.filename,
                        "input_size": input_info.file_size,
                        "expected_size": output_info.file_size,
                        "input_path": f"{zip_relative}::{input_info.filename}",
                        "expected_path": f"{zip_relative}::{output_info.filename}",
                    }
                )
            if not pairs:
                return recognizer, [], ["recognized_layout_contains_no_complete_input_output_pairs"]
            return recognizer, pairs, evidence
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return None, [], [f"zip_read_failed:{type(exc).__name__}:{exc}"]


def _mapped_tests(
    zip_path: Path,
    zip_relative: str,
    row: dict[str, Any],
    subtask_names: set[str],
    mapping_path: Path,
) -> tuple[list[TestCase], str | None]:
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"subtask_map_read_failed:{type(exc).__name__}:{exc}"
    if not isinstance(mapping, dict):
        return [], "subtask_map_rule:document_must_be_object"
    if mapping.get("problem_id") != row["id"]:
        return [], f"subtask_map_rule:problem_id_mismatch:{mapping.get('problem_id')}"
    if mapping.get("zip") != zip_relative:
        return [], f"subtask_map_rule:zip_mismatch:{mapping.get('zip')}!={zip_relative}"
    members = mapping.get("members")
    excluded = mapping.get("excluded")
    if not isinstance(members, list) or not isinstance(excluded, list):
        return [], "subtask_map_rule:members_and_excluded_must_be_lists"

    try:
        with zipfile.ZipFile(zip_path) as archive:
            zip_members = {info.filename for info in archive.infolist() if not info.is_dir()}
            _, recognized = _recognized_members(archive, row)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return [], f"subtask_map_zip_read_failed:{type(exc).__name__}:{exc}"

    recognized_names = {info.filename for info in recognized}
    input_names = {
        info.filename
        for info in recognized
        if info.filename.rsplit(".", 1)[-1].casefold() == "in"
    }
    seen_inputs: list[str] = []
    tests: list[TestCase] = []
    assigned_subtasks: set[str] = set()
    for index, item in enumerate(members):
        if not isinstance(item, dict):
            return [], f"subtask_map_rule:members[{index}]_must_be_object"
        input_member = item.get("input")
        expected_member = item.get("expected")
        subtask = item.get("subtask")
        if not isinstance(input_member, str) or not isinstance(expected_member, str) or not isinstance(subtask, str):
            return [], f"subtask_map_rule:members[{index}]_requires_input_expected_subtask_strings"
        if input_member not in zip_members:
            return [], f"subtask_map_rule:missing_zip_member:{input_member}"
        if expected_member not in zip_members:
            return [], f"subtask_map_rule:missing_zip_member:{expected_member}"
        if input_member not in input_names:
            return [], f"subtask_map_rule:input_not_in_recognized_test_directory:{input_member}"
        if expected_member not in recognized_names or expected_member.rsplit(".", 1)[-1].casefold() not in {"out", "a"}:
            return [], f"subtask_map_rule:expected_not_in_recognized_test_directory:{expected_member}"
        if input_member.rsplit(".", 1)[0] != expected_member.rsplit(".", 1)[0]:
            return [], f"subtask_map_rule:input_expected_stem_mismatch:{input_member}:{expected_member}"
        if subtask not in subtask_names:
            return [], f"subtask_map_rule:unknown_subtask:{subtask}"
        seen_inputs.append(input_member)
        assigned_subtasks.add(subtask)
        tests.append(TestCase(str(row["id"]), subtask, f"{zip_relative}::{input_member}", f"{zip_relative}::{expected_member}"))

    excluded_names: list[str] = []
    for index, item in enumerate(excluded):
        if not isinstance(item, dict) or not isinstance(item.get("member"), str):
            return [], f"subtask_map_rule:excluded[{index}]_requires_member_string"
        member = item["member"]
        if member not in zip_members:
            return [], f"subtask_map_rule:missing_zip_member:{member}"
        if member not in input_names:
            return [], f"subtask_map_rule:excluded_member_not_input:{member}"
        excluded_names.append(member)

    accounted = seen_inputs + excluded_names
    repeated = sorted({member for member in accounted if accounted.count(member) > 1})
    if repeated:
        return [], f"subtask_map_rule:input_member_not_exactly_once:{','.join(repeated)}"
    missing = sorted(input_names - set(accounted))
    extra = sorted(set(accounted) - input_names)
    if missing or extra:
        return [], f"subtask_map_rule:input_coverage_mismatch:missing={missing};extra={extra}"
    empty_subtasks = sorted(subtask_names - assigned_subtasks)
    if empty_subtasks:
        return [], f"subtask_map_rule:subtasks_without_tests:{','.join(empty_subtasks)}"
    return sorted(tests, key=lambda test: (test.input_path, test.expected_path, test.subtask)), None


def _sample_member_from_zip(
    zip_path: Path, row: dict[str, Any], corpus: Path, input_only: bool = False
) -> tuple[bool, str | None]:
    statement, _ = _load_statement(corpus, row)
    if statement is None:
        return False, None
    statement_path = corpus / str(row.get("source_html", ""))
    if not statement_path.is_file():
        statement_path = corpus.parent / str(row.get("source_html", ""))
    samples = extract_samples(statement_path.read_text(encoding="utf-8", errors="replace"), *problem_key(row))
    if not samples:
        return False, None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            _, recognized = _recognized_members(archive, row)
            groups: dict[str, dict[str, zipfile.ZipInfo]] = defaultdict(dict)
            for info in recognized:
                extension = info.filename.rsplit(".", 1)[-1].casefold()
                kind = PAIR_EXTENSIONS[extension]
                stem = info.filename.rsplit(".", 1)[0]
                groups[stem]["expected" if kind == "expected" else kind] = info
            pairs = [
                {"input_name": group["input"].filename, "expected_name": group["expected"].filename}
                for _, group in sorted(groups.items())
                if "input" in group and "expected" in group
            ]
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return True, None
    return True, sample_matches(samples, pairs, zip_path, input_only)


CHECKER_PROOF = "checker-proof.md"
CHECKER_CLASSES = {"any-valid", "scorer"}
PROOF_MUTATIONS = ("truncate_tail", "change_one_value", "violate_constraint")
_PROOF_CONCLUSION = re.compile(r"^Conclusion: (admitted|rejected)\s*$", re.MULTILINE)
_PROOF_OFFICIAL = re.compile(r"^\| Official outputs accepted \| (\d+) of (\d+) \|\s*$", re.MULTILINE)
_PROOF_OFFICIAL_REJECTED = re.compile(r"^\| Official outputs rejected \| (\d+) \|\s*$", re.MULTILINE)
_PROOF_MUTATION = r"^\| Mutation `{kind}` rejected \| (\d+) of (\d+) tried \(accepted (\d+), crashes (\d+)\) \|\s*$"


def checker_proven(problem_dir: Path | None) -> bool:
    """True when checker.py exists and checker-proof.md records an admitted proof that meets the rule.

    A missing or unreadable summary means "not proven", never an error.
    """
    if problem_dir is None:
        return False
    checker_path = problem_dir / "checker.py"
    proof_path = problem_dir / CHECKER_PROOF
    if not checker_path.is_file() or not proof_path.is_file():
        return False
    try:
        text = proof_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    conclusion = _PROOF_CONCLUSION.search(text)
    official = _PROOF_OFFICIAL.search(text)
    official_rejected = _PROOF_OFFICIAL_REJECTED.search(text)
    if not conclusion or conclusion.group(1) != "admitted" or not official or not official_rejected:
        return False
    accepted, total = int(official.group(1)), int(official.group(2))
    if total < 1 or accepted != total or int(official_rejected.group(1)) != 0:
        return False
    for kind in PROOF_MUTATIONS:
        match = re.search(_PROOF_MUTATION.format(kind=kind), text, re.MULTILINE)
        if not match:
            return False
        rejected, _tried, accepted_mutants, crashes = map(int, match.groups())
        if rejected < 3 or accepted_mutants != 0 or crashes != 0:
            return False
    return True


def _status_reason(row: dict[str, Any], checker_ok: bool = False) -> tuple[GradingKind, RejectionReason | None] | None:
    checker = row.get("checker_class")
    if checker_ok and checker in CHECKER_CLASSES and row.get("io_mode") != "output-only":
        return (GradingKind.VALIDATOR if checker == "any-valid" else GradingKind.SCORER), None
    io_mode = row.get("io_mode")
    if io_mode == "output-only":
        return GradingKind.SCORER, RejectionReason.OUTPUT_ONLY
    if checker == "interactive" or io_mode == "interactive":
        return GradingKind.INTERACTIVE, RejectionReason.INTERACTIVE
    if checker == "any-valid":
        return GradingKind.VALIDATOR, RejectionReason.NEEDS_VALIDATOR
    if checker == "scorer":
        return GradingKind.SCORER, RejectionReason.NEEDS_SCORER
    if checker in {"float-tol", "float-format"}:
        return GradingKind.FLOAT_TOL, None
    if checker == "exact":
        return GradingKind.EXACT, None
    return None


def load_dmoj_limits(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        entry["problem_id"]: entry
        for entry in document["problems"]
        if entry.get("memory_limit_mb") is not None and entry.get("dmoj_code")
    }


def _memory_from_statement(statement: str, requirement: str | None) -> int | None:
    match = re.search(r"Memory\s+Limit\s*:\s*([\d,]+)\s*(KB|MB|GB)", statement, re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1).replace(",", ""))
    unit = match.group(2).upper()
    multiplier = {"KB": 1 / 1024, "MB": 1, "GB": 1024}[unit]
    parsed = round(amount * multiplier)
    if requirement and requirement.casefold() != "unknown":
        stated = re.search(r"([\d,]+)\s*(KB|MB|GB)", requirement, re.IGNORECASE)
        if stated:
            expected = round(int(stated.group(1).replace(",", "")) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[stated.group(2).upper()])
            if expected != parsed:
                return None
    return parsed


def _checked_at(zip_path: Path | None, checked_at: str | None) -> str:
    if checked_at:
        return checked_at
    if zip_path is None or not zip_path.is_file():
        raise ValueError("--checked-at is required when a problem ZIP is missing")
    return datetime.fromtimestamp(zip_path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _jsonl_bytes(records: list[Any]) -> bytes:
    return "".join(
        json.dumps(asdict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")


def _load_statement(corpus: Path, row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    source_path = str(row["source_html"])
    statement_path = corpus / source_path
    if not statement_path.is_file():
        repository_path = corpus.parent / source_path
        if repository_path.is_file():
            statement_path = repository_path
    try:
        problems, limitations = parse_problem_html(str(row["year"]), statement_path)
    except OSError as exc:
        return None, f"statement_read_failed:{exc}"
    day, day_problem = problem_key(row)
    match = next((item for item in problems if item.get("day") == day and item.get("day_problem") == day_problem), None)
    if match is None:
        return None, f"statement_problem_not_found:{day}:{day_problem};limitations={limitations}"
    if not match.get("statement") or not match.get("title"):
        return None, "statement_missing_title_or_text"
    return match, None


def admit_row(
    row: dict[str, Any],
    zips_root: Path,
    corpus: Path,
    checked_at: str | None = None,
    dmoj_limits: dict[str, dict[str, Any]] | None = None,
    problem_dir: Path | None = None,
) -> tuple[Problem, list[TestCase], Admission]:
    problem_id = str(row["id"])
    zip_path = locate_zip(zips_root, int(row["year"]))
    timestamp = _checked_at(zip_path, checked_at)
    statement, statement_error = _load_statement(corpus, row)
    statement_path = str(row.get("source_html", ""))
    checker_ok = checker_proven(problem_dir)
    grading = _status_reason(row, checker_ok)
    if statement is None:
        problem = Problem(problem_id, int(row["year"]), int(row["day"]), str(row["title"]), statement_path, float(row["time_limit_seconds"]), None, None, _weights(row), grading[0] if grading else GradingKind.EXACT)
        status = Admission(problem_id, AdmissionStatus.REJECTED, RejectionReason.STATEMENT_UNPARSEABLE, [statement_error or "statement_unparseable"], None, timestamp)
        return problem, [], status

    statement_text = str(statement["statement"])
    memory = _memory_from_statement(statement_text, row.get("memory_limit_stated"))
    memory_source = statement_path if memory is not None else None
    dmoj = (dmoj_limits or {}).get(problem_id)
    if memory is None and dmoj is not None:
        memory = int(dmoj["memory_limit_mb"])
        memory_source = f"dmoj:{dmoj['dmoj_code']}"
    grading_kind = grading[0] if grading else GradingKind.EXACT
    problem = Problem(
        id=problem_id,
        year=int(row["year"]),
        day=int(row["day"]),
        title=str(statement["title"]),
        statement_path=statement_path,
        time_limit_s=float(row["time_limit_seconds"]),
        memory_limit_mb=memory,
        memory_limit_source=memory_source,
        subtasks=_weights(row),
        grading_kind=grading_kind,
    )
    evidence = [statement_path, "requirements:problem-requirements.jsonl"]
    if zip_path is None or not zip_path.is_file():
        evidence.append(f"missing_zip_for_year:{row['year']}")
        status = Admission(problem_id, AdmissionStatus.REJECTED, RejectionReason.TEST_DATA_MISSING, evidence, None, timestamp)
        return problem, [], status

    zip_relative = zip_path.relative_to(zips_root).as_posix()
    evidence.append(zip_relative)
    recognizer, pairs, pair_evidence = discover_pairs(zip_path, zip_relative, row, problem.subtasks)
    evidence.extend(pair_evidence)
    test_cases = [
        TestCase(problem_id, pair["subtask"], pair["input_path"], pair["expected_path"])
        for pair in pairs
        if "sample" not in PurePosixPath(pair["input_name"]).name.casefold()
    ]
    unsupported = grading[1] if grading else None
    if unsupported is not None:
        evidence.append(f"checker_class:{row.get('checker_class')};io_mode:{row.get('io_mode')}")
        if problem_dir is not None and (problem_dir / CHECKER_PROOF).is_file() and not checker_ok:
            evidence.append(f"checker_proof_failed_or_stale:{CHECKER_PROOF}")
        status = Admission(problem_id, AdmissionStatus.REJECTED, unsupported, evidence, None, timestamp)
        return problem, test_cases, status

    if recognizer is None or not pairs or not test_cases:
        status = Admission(problem_id, AdmissionStatus.PENDING, RejectionReason.UNCLEAR, evidence, None, timestamp)
        return problem, test_cases, status

    if grading is None:
        evidence.append(f"unrecognized_checker_class:{row.get('checker_class')}")
        status = Admission(problem_id, AdmissionStatus.PENDING, RejectionReason.UNCLEAR, evidence, None, timestamp)
        return problem, test_cases, status

    html_path = corpus / statement_path
    if not html_path.is_file():
        html_path = corpus.parent / statement_path
    samples = extract_samples(html_path.read_text(encoding="utf-8", errors="replace"), *problem_key(row))
    input_only = checker_ok and grading is not None and grading[0] in (GradingKind.VALIDATOR, GradingKind.SCORER)
    sample_member = sample_matches(samples, pairs, zip_path, input_only)
    if sample_member is None:
        reason = RejectionReason.SAMPLES_DISAGREE if samples else RejectionReason.UNCLEAR
        evidence.append("no_sample_pair_matched_zip_after_whitespace_normalization" if samples else "statement_sample_extraction_found_no_input_output_pairs")
        status = Admission(problem_id, AdmissionStatus.REJECTED if samples else AdmissionStatus.PENDING, reason, evidence, None, timestamp)
        return problem, test_cases, status
    evidence.append(f"matched_statement_sample:{zip_relative}::{sample_member}")
    if input_only:
        evidence.append(f"checker_proof:{CHECKER_PROOF}")
    status = Admission(problem_id, AdmissionStatus.ADMITTED, None, evidence, None, timestamp)
    return problem, test_cases, status


def load_requirements(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


SAMPLE_CONFLICT_DETAIL = "worker verdict is admitted but no statement sample pair matched the ZIP after whitespace normalization"
SAMPLE_OVERRIDE_EVIDENCE = "admission_override:worker_admitted_but_no_sample_pair_matched_zip"


def run_admission(
    zips_root: Path,
    corpus: Path,
    requirements: Path,
    out: Path,
    checked_at: str | None = None,
    dmoj_limits_path: Path | None = None,
) -> None:
    if dmoj_limits_path is None:
        dmoj_limits_path = requirements.parent / "dmoj-memory-limits.json"
    dmoj_limits = load_dmoj_limits(dmoj_limits_path)
    sample_conflicts: list[dict[str, str]] = []
    for row in load_requirements(requirements):
        problem_dir = out / str(row["id"])
        checker_ok = checker_proven(problem_dir)
        problem, tests, status = admit_row(row, zips_root, corpus, checked_at, dmoj_limits, problem_dir)
        problem_dir.mkdir(parents=True, exist_ok=True)
        status_path = problem_dir / "status.json"
        map_path = problem_dir / "subtask_map.json"
        worker_status: dict[str, Any] | None = None
        if status_path.is_file():
            try:
                loaded_status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded_status = None
            if isinstance(loaded_status, dict) and loaded_status.get("worker_model") is not None:
                worker_status = loaded_status

        if map_path.is_file():
            zip_path = locate_zip(zips_root, int(row["year"]))
            if zip_path is None or not zip_path.is_file():
                mapped_tests, map_error = [], f"subtask_map_rule:zip_missing_for_year:{row['year']}"
                zip_relative = None
            else:
                zip_relative = zip_path.relative_to(zips_root).as_posix()
                mapped_tests, map_error = _mapped_tests(
                    zip_path,
                    zip_relative,
                    row,
                    {subtask.name for subtask in problem.subtasks},
                    map_path,
                )
            if map_error is not None:
                if worker_status is not None:
                    if worker_status.get("status") == AdmissionStatus.ADMITTED:
                        raise ValueError(f"worker-admitted problem {problem.id} has invalid subtask_map: {map_error}")
                    tests = []
                else:
                    status = Admission(
                        problem.id,
                        AdmissionStatus.REJECTED,
                        RejectionReason.TEST_LAYOUT_UNRECOGNIZED,
                        [*status.evidence, map_error],
                        None,
                        status.checked_at,
                    )
                    tests = []
            else:
                tests = mapped_tests
                if worker_status is None:
                    unsupported = _status_reason(row, checker_ok)
                    if unsupported is None:
                        status = Admission(
                            problem.id,
                            AdmissionStatus.PENDING,
                            RejectionReason.UNCLEAR,
                            [*status.evidence, "unrecognized_checker_class"],
                            None,
                            status.checked_at,
                        )
                    elif unsupported[1] is not None:
                        status = Admission(
                            problem.id,
                            AdmissionStatus.REJECTED,
                            unsupported[1],
                            [*status.evidence, "subtask_map:validated"],
                            None,
                            status.checked_at,
                        )
                    else:
                        has_samples, sample_member = _sample_member_from_zip(zip_path, row, corpus, checker_ok) if zip_path else (False, None)
                        if sample_member is not None:
                            status = Admission(
                                problem.id,
                                AdmissionStatus.ADMITTED,
                                None,
                                [
                                    *status.evidence,
                                    "subtask_map:validated",
                                    f"matched_statement_sample:{zip_relative}::{sample_member}",
                                    *([f"checker_proof:{CHECKER_PROOF}"] if checker_ok else []),
                                ],
                                None,
                                status.checked_at,
                            )
                        elif has_samples:
                            status = Admission(
                                problem.id,
                                AdmissionStatus.REJECTED,
                                RejectionReason.SAMPLES_DISAGREE,
                                [*status.evidence, "subtask_map:validated", "no_sample_pair_matched_zip_after_whitespace_normalization"],
                                None,
                                status.checked_at,
                            )
                        else:
                            status = Admission(
                                problem.id,
                                AdmissionStatus.PENDING,
                                RejectionReason.UNCLEAR,
                                [*status.evidence, "subtask_map:validated", "statement_sample_extraction_found_no_input_output_pairs"],
                                None,
                                status.checked_at,
                            )
        elif worker_status is not None and worker_status.get("status") == AdmissionStatus.ADMITTED:
            raise ValueError(f"worker-admitted problem {problem.id} has no subtask_map.json")

        (problem_dir / "problem.json").write_bytes(_json_bytes(asdict(problem)))
        if worker_status is None:
            (status_path).write_bytes(_json_bytes(asdict(status)))
        else:
            if map_path.is_file() and map_error is None:
                was_admitted = worker_status.get("status") == AdmissionStatus.ADMITTED
                if was_admitted or SAMPLE_OVERRIDE_EVIDENCE in worker_status.get("evidence", []):
                    has_samples, sample_member = _sample_member_from_zip(zip_path, row, corpus) if zip_path else (False, None)
                    if has_samples and sample_member is None:
                        sample_conflicts.append({"problem_id": problem.id, "detail": SAMPLE_CONFLICT_DETAIL})
                        if was_admitted:
                            overridden = {**worker_status, "status": AdmissionStatus.REJECTED.value,
                                          "reason": RejectionReason.SAMPLES_DISAGREE.value,
                                          "evidence": [*worker_status.get("evidence", []), SAMPLE_OVERRIDE_EVIDENCE]}
                            status_path.write_bytes(_json_bytes(overridden))
            if not status_path.is_file():
                status_path.write_bytes(_json_bytes(asdict(status)))
        (problem_dir / "tests.jsonl").write_bytes(_jsonl_bytes(tests))
    (out / "admission-report.json").write_bytes(_json_bytes({"sample_conflicts": sample_conflicts}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zips-root", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--dmoj-limits",
        type=Path,
        help="DMOJ memory limits JSON; defaults to dmoj-memory-limits.json beside --requirements when present.",
    )
    parser.add_argument("--checked-at", help="Fixed UTC timestamp; required if any problem ZIP is missing.")
    args = parser.parse_args()
    run_admission(args.zips_root, args.corpus, args.requirements, args.out, args.checked_at, args.dmoj_limits)


if __name__ == "__main__":
    main()

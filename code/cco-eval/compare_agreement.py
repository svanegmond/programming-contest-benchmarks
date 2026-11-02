#!/usr/bin/env python3
"""Compare Gemini Flash agreement maps with admitted Sonnet subtask maps."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_PROBLEMS = ("cco-2016-d1p1", "cco-2017-d2p1", "cco-2019-d2p2")


def load_map(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        data = json.load(source)

    members = data.get("members")
    excluded = data.get("excluded", [])
    if not isinstance(members, list) or not isinstance(excluded, list):
        raise ValueError(f"{path}: expected 'members' and 'excluded' arrays")

    mapped: dict[str, dict[str, str]] = {}
    for member in members:
        input_path = member.get("input")
        subtask = member.get("subtask")
        expected = member.get("expected")
        if not input_path or not subtask or not expected:
            raise ValueError(f"{path}: each mapped member needs input, expected, and subtask")
        if input_path in mapped:
            raise ValueError(f"{path}: duplicate input member {input_path!r}")
        mapped[input_path] = {"subtask": subtask, "expected": expected}

    excluded_members: set[str] = set()
    for member in excluded:
        name = member.get("member")
        if not name:
            raise ValueError(f"{path}: each excluded member needs a member path")
        if name in excluded_members:
            raise ValueError(f"{path}: duplicate excluded member {name!r}")
        excluded_members.add(name)

    overlap = mapped.keys() & excluded_members
    if overlap:
        raise ValueError(f"{path}: members both mapped and excluded: {sorted(overlap)}")
    return {"mapped": mapped, "excluded": excluded_members}


def cohen_kappa(left: list[str], right: list[str]) -> float | None:
    if not left:
        return None
    if len(left) != len(right):
        raise ValueError("Cohen's kappa inputs must have equal lengths")

    observed = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    left_counts = Counter(left)
    right_counts = Counter(right)
    labels = left_counts.keys() | right_counts.keys()
    expected = sum(left_counts[label] * right_counts[label] for label in labels) / len(left) ** 2
    if expected == 1:
        return 1.0 if observed == 1 else None
    return (observed - expected) / (1 - expected)


def compare_maps(problem_id: str, gemini: dict[str, Any], sonnet: dict[str, Any]) -> dict[str, Any]:
    gmap, smap = gemini["mapped"], sonnet["mapped"]
    gexcluded, sexcluded = gemini["excluded"], sonnet["excluded"]
    all_names = gmap.keys() | smap.keys() | gexcluded | sexcluded

    same: list[str] = []
    different: list[dict[str, str]] = []
    pair_differences: list[dict[str, str]] = []
    g_labels: list[str] = []
    s_labels: list[str] = []
    for name in sorted(gmap.keys() & smap.keys()):
        g_label, s_label = gmap[name]["subtask"], smap[name]["subtask"]
        g_labels.append(g_label)
        s_labels.append(s_label)
        if g_label == s_label:
            same.append(name)
        else:
            different.append({"input": name, "gemini_flash": g_label, "sonnet": s_label})
        if gmap[name]["expected"] != smap[name]["expected"]:
            pair_differences.append({
                "input": name,
                "gemini_expected": gmap[name]["expected"],
                "sonnet_expected": smap[name]["expected"],
            })

    excluded_by_one: list[dict[str, str]] = []
    for name in sorted(gexcluded ^ sexcluded):
        if name in gexcluded:
            excluded_by, other = "Gemini Flash", smap.get(name)
        else:
            excluded_by, other = "Sonnet", gmap.get(name)
        classification = f"mapped:{other['subtask']}" if other else "not listed"
        excluded_by_one.append({
            "member": name,
            "excluded_by": excluded_by,
            "other_classification": classification,
        })

    classifications_match = all(
        (name in gmap and name in smap and gmap[name]["subtask"] == smap[name]["subtask"])
        or (name in gexcluded and name in sexcluded)
        for name in all_names
    )
    compared = len(g_labels)
    agreements = len(same)
    return {
        "problem_id": problem_id,
        "verdict_match": classifications_match and not pair_differences,
        "mapped_same": same,
        "mapped_differently": different,
        "excluded_by_one_only": excluded_by_one,
        "paired_output_differences": pair_differences,
        "compared_mapped_members": compared,
        "agreements": agreements,
        "percent_agreement": (100 * agreements / compared) if compared else None,
        "cohen_kappa": cohen_kappa(g_labels, s_labels),
        "_gemini_labels": g_labels,
        "_sonnet_labels": s_labels,
    }


def compare_problem(root: Path, problem_id: str) -> dict[str, Any]:
    return compare_maps(
        problem_id,
        load_map(root / "agreement" / problem_id / "subtask_map.json"),
        load_map(root / "problems" / problem_id / "subtask_map.json"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("problem_ids", nargs="*", default=DEFAULT_PROBLEMS)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (defaults to the parent of code/)",
    )
    args = parser.parse_args()
    try:
        results = [compare_problem(args.root, problem_id) for problem_id in args.problem_ids]
        total = sum(result["compared_mapped_members"] for result in results)
        agreements = sum(result["agreements"] for result in results)
        results.append({
            "problem_id": "overall",
            "verdict_match": all(result["verdict_match"] for result in results),
            "compared_mapped_members": total,
            "agreements": agreements,
            "percent_agreement": (100 * agreements / total) if total else None,
            "cohen_kappa": cohen_kappa(
                [label for result in results for label in result.get("_gemini_labels", [])],
                [label for result in results for label in result.get("_sonnet_labels", [])],
            ),
        })
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    for result in results:
        result.pop("_gemini_labels", None)
        result.pop("_sonnet_labels", None)
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

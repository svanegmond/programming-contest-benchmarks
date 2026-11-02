import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compare_agreement import compare_maps, cohen_kappa


def test_compares_assignments_exclusions_and_kappa():
    gemini = {
        "mapped": {
            "same.in": {"subtask": "one", "expected": "same.out"},
            "different.in": {"subtask": "one", "expected": "different.out"},
        },
        "excluded": {"only-gemini.in"},
    }
    sonnet = {
        "mapped": {
            "same.in": {"subtask": "one", "expected": "same.out"},
            "different.in": {"subtask": "two", "expected": "different.out"},
            "only-gemini.in": {"subtask": "one", "expected": "only-gemini.out"},
        },
        "excluded": set(),
    }

    result = compare_maps("fixture", gemini, sonnet)

    assert result["verdict_match"] is False
    assert result["mapped_same"] == ["same.in"]
    assert result["mapped_differently"] == [
        {"input": "different.in", "gemini_flash": "one", "sonnet": "two"}
    ]
    assert result["excluded_by_one_only"] == [
        {
            "member": "only-gemini.in",
            "excluded_by": "Gemini Flash",
            "other_classification": "mapped:one",
        }
    ]
    assert result["percent_agreement"] == 50
    assert result["cohen_kappa"] == 0


def test_kappa_is_one_for_perfect_agreement_and_undefined_without_members():
    assert cohen_kappa(["one", "two"], ["one", "two"]) == 1
    assert cohen_kappa([], []) is None

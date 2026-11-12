from __future__ import annotations

from pathlib import Path

from runners.judge_client import StubJudge
from runners.selection import (
    SampleProgram, cluster_select, parse_cluster_inputs,
)
from runners.common import ProblemData


def _program(root: Path, index: int, source: str) -> SampleProgram:
    path = root / f"s{index}.py"
    path.write_text(source, encoding="utf-8")
    return SampleProgram(index, "python", path, path.parent)


def test_parse_cluster_inputs_from_fenced_in_blocks():
    text = "```in\n1 2\n```\n```stdin\n3 4\n```"
    assert parse_cluster_inputs(text) == ["1 2\n", "3 4\n"]


def test_cluster_drops_sample_failures_and_picks_the_largest_group(tmp_path):
    (tmp_path / "ok.py").write_text(
        "import sys\na,b=map(int,sys.stdin.read().split()); print(a+b)\n", encoding="utf-8")
    (tmp_path / "wrong.py").write_text("print(0)\n", encoding="utf-8")
    problem = ProblemData(
        "p", 2024, 1, "Sum", "sum", None, 1, 128, {}, None, None,
        samples=[("2 3\n", "5\n")],
    )
    correct = "import sys\na,b=map(int,sys.stdin.read().split()); print(a+b)\n"
    programs = [
        _program(tmp_path, 1, "print(0)\n"),
        _program(tmp_path, 2, correct),
        _program(tmp_path, 3, correct),
    ]
    decision = cluster_select(
        programs, problem=problem, judge=StubJudge(), extra_inputs=["9 1\n", "4 4\n"],
    )
    assert decision.dropped[0]["sample_index"] == 1
    assert decision.chosen_index == 2
    assert decision.groups[0]["size"] == 2
    assert "largest_cluster" in decision.reason
    assert decision.extra_input_source == "model"

"""AlphaCode-style filter-and-cluster selection (off by default)."""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from cco_eval.waterloo import Language
from runners.common import ProblemData, run_model

INPUT_FENCE = re.compile(r"```\s*(?:in|stdin|input)\s*\n(.*?)```", re.I | re.S)


@dataclass
class SampleProgram:
    sample_index: int
    language: str | None
    code_path: Path | None
    artifact: Path


@dataclass
class ClusterDecision:
    chosen_index: int | None
    reason: str
    dropped: list[dict[str, object]] = field(default_factory=list)
    groups: list[dict[str, object]] = field(default_factory=list)
    extra_input_count: int = 0
    extra_input_source: str = "none"

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def parse_cluster_inputs(text: str) -> list[str]:
    return [match.group(1) for match in INPUT_FENCE.finditer(text) if match.group(1).strip()]


def generate_cluster_inputs(
    problem: ProblemData,
    model: str,
    *,
    timeout: float,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str | None,
    catalog: dict[str, Any] | None = None,
    generate: Callable[..., Any] = run_model,
) -> list[str]:
    prompt = (
        "These are published past Canadian Computing Olympiad problems used to evaluate models. "
        "Write 3 small additional stdin test cases that are not the official samples. "
        "Each case is a fenced block tagged `in`.\n\n"
        f"Problem: {problem.id} - {problem.title}\n\n{problem.statement}"
    )
    result = generate(
        model,
        [{"role": "user", "content": prompt}],
        None,
        timeout,
        max_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        catalog=catalog,
    )
    return parse_cluster_inputs(getattr(result, "text", "") or "")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _run_stdin(judge: Any, language: str, code_path: Path, stdin: str, time_limit_s: float,
               memory_mb: int | None, attempt_id: str) -> str:
    with tempfile.TemporaryDirectory(prefix="cco-cluster-") as temp:
        stdin_path = Path(temp) / "stdin.txt"
        stdin_path.write_text(stdin if stdin.endswith("\n") else stdin + "\n", encoding="utf-8")
        result = judge.run(
            language, code_path, stdin_path, time_limit_s,
            attempt_id=attempt_id, memory_mb=memory_mb, output_dir=Path(temp),
        )
        if result.get("verdict") not in {"ran", "completed"} or not result.get("compile", {}).get("ok", True):
            return f"__error__:{result.get('verdict')}"
        stdout_path = result.get("stdout_path")
        if stdout_path and Path(str(stdout_path)).is_file():
            return _normalize(Path(str(stdout_path)).read_text(encoding="utf-8", errors="replace"))
        return _normalize(str(result.get("stdout_head") or ""))


def pass_official_samples(judge: Any, program: SampleProgram, problem: ProblemData) -> tuple[bool, str]:
    if not program.code_path or not program.language or not program.code_path.is_file():
        return False, "no_program"
    for index, (stdin, expected) in enumerate(problem.samples):
        got = _run_stdin(
            judge, program.language, program.code_path, stdin, problem.time_limit_s,
            problem.memory_limit_mb, f"cluster-{program.sample_index}-sample{index}",
        )
        if got.startswith("__error__:"):
            return False, got
        if _normalize(got) != _normalize(expected):
            return False, f"sample_{index}_mismatch"
    return True, "ok"


def cluster_select(
    programs: list[SampleProgram],
    *,
    problem: ProblemData,
    judge: Any,
    extra_inputs: list[str],
) -> ClusterDecision:
    dropped: list[dict[str, object]] = []
    survivors: list[SampleProgram] = []
    for program in programs:
        ok, detail = pass_official_samples(judge, program, problem)
        if not ok:
            dropped.append({"sample_index": program.sample_index, "reason": detail})
            continue
        survivors.append(program)
    if not survivors:
        return ClusterDecision(
            None, "all_samples_dropped", dropped, [], len(extra_inputs),
            "model" if extra_inputs else "none",
        )
    inputs = extra_inputs if extra_inputs else [stdin for stdin, _ in problem.samples]
    source = "model" if extra_inputs else "official_samples"
    grouped: dict[tuple[str, ...], list[int]] = {}
    for program in survivors:
        fingerprint = tuple(
            _run_stdin(
                judge, str(program.language), program.code_path, stdin,  # type: ignore[arg-type]
                problem.time_limit_s, problem.memory_limit_mb,
                f"cluster-{program.sample_index}-extra{index}",
            )
            for index, stdin in enumerate(inputs)
        )
        grouped.setdefault(fingerprint, []).append(program.sample_index)
    groups = sorted(grouped.values(), key=lambda members: (-len(members), min(members)))
    chosen = min(groups[0])
    reason = (
        f"largest_cluster size={len(groups[0])} samples={groups[0]} "
        f"extra_inputs={len(extra_inputs)} source={source}"
    )
    return ClusterDecision(
        chosen, reason, dropped,
        [{"size": len(members), "samples": members} for members in groups],
        len(extra_inputs), source,
    )


def programs_from_results(results: list[dict[str, Any]]) -> list[SampleProgram]:
    programs: list[SampleProgram] = []
    for result in results:
        attempt = result["attempt"]
        language = attempt.get("language")
        if isinstance(language, Language):
            language = language.value
        code_path = attempt.get("code_path")
        programs.append(SampleProgram(
            sample_index=int(attempt["sample_index"]),
            language=str(language) if language else None,
            code_path=Path(code_path) if code_path else None,
            artifact=Path(str(result["artifact"])),
        ))
    return programs

"""Record shapes for the Waterloo coding benchmark (brief: project/briefs/u-waterloo-coding-evals.md).

Every component (admission, judge, runners, platform sinks, dashboard) reads and
writes these shapes as JSON. Change them here, not at call sites.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class GradingKind(StrEnum):
    EXACT = "exact"
    FLOAT_TOL = "float_tol"
    VALIDATOR = "validator"
    SCORER = "scorer"
    INTERACTIVE = "interactive"


class AdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    REJECTED = "rejected"
    PENDING = "pending"


class RejectionReason(StrEnum):
    INTERACTIVE = "interactive"
    NEEDS_VALIDATOR = "needs_validator"
    NEEDS_SCORER = "needs_scorer"
    OUTPUT_ONLY = "output_only"
    STATEMENT_UNPARSEABLE = "statement_unparseable"
    TEST_DATA_MISSING = "test_data_missing"
    TEST_LAYOUT_UNRECOGNIZED = "test_layout_unrecognized"
    SAMPLES_DISAGREE = "samples_disagree"
    UNCLEAR = "unclear"


class Mode(StrEnum):
    SINGLE_SHOT = "single_shot"
    AGENTIC = "agentic"


class Lane(StrEnum):
    CPP = "cpp"
    PYTHON = "python"
    FREE = "free"


class Language(StrEnum):
    CPP = "cpp"
    PYTHON = "python"


class AgentOutcome(StrEnum):
    SUBMITTED = "submitted"
    WALL_CLOCK_EXHAUSTED = "wall_clock_exhausted"  # historical; new runs never emit this
    TURN_BUDGET_EXHAUSTED = "turn_budget_exhausted"
    NO_SOLUTION_WRITTEN = "no_solution_written"
    HARNESS_ERROR = "harness_error"


class TestVerdict(StrEnum):
    PASS = "pass"
    WRONG_ANSWER = "wrong_answer"
    RUNTIME_ERROR = "runtime_error"
    KILLED = "killed"
    COMPILE_ERROR = "compile_error"


class Outcome3(StrEnum):
    PASS_ON_TIME = "pass_on_time"
    PASS_OVER_TIME = "pass_over_time"
    FAIL = "fail"


class FailureKind(StrEnum):
    TRUNCATED = "truncated"
    NO_PROGRAM = "no_program"


# Per-test sandbox give-up. Statement time_limit_s is recorded as over_time, not a fail.
GIVE_UP_TIMEOUT_S = 120.0


def sandbox_timeout_s(time_limit_s: float, give_up_s: float | None = None) -> float:
    limit = float(time_limit_s)
    give_up = GIVE_UP_TIMEOUT_S if give_up_s is None else float(give_up_s)
    if limit <= 0 or give_up <= 0:
        raise ValueError("timeouts must be positive")
    return max(limit, give_up)


@dataclass
class Subtask:
    name: str
    weight: float


@dataclass
class Problem:
    id: str
    year: int
    day: int
    title: str
    statement_path: str
    time_limit_s: float
    memory_limit_mb: int | None
    memory_limit_source: str | None
    subtasks: list[Subtask]
    grading_kind: GradingKind
    absolute_tolerance: float | None = None
    relative_tolerance: float | None = None


@dataclass
class TestCase:
    problem_id: str
    subtask: str
    input_path: str
    expected_path: str


@dataclass
class Admission:
    problem_id: str
    status: AdmissionStatus
    reason: RejectionReason | None
    evidence: list[str]
    worker_model: str | None
    checked_at: str


@dataclass
class Budget:
    max_turns: int
    wall_clock_s: float | None = None


@dataclass
class RunManifest:
    run_id: str
    git_sha: str
    env_id: str
    cohort_hash: str
    models: list[str]
    mode: Mode
    caps: dict[str, object]
    budget: Budget | None
    problem_ids: list[str]
    timeout_s: float
    judge: dict[str, str]
    k: int = 1
    temperature: float = 0.0
    reasoning_effort: str | None = "high"
    lane: str = "cpp"
    selection: str = "pass_at_k"
    prompt_version: str = "cco-v3"
    multi_submit: bool = False


@dataclass
class Attempt:
    id: str
    problem_id: str
    mode: Mode
    model: str
    provider: str
    language: Language | None
    env_id: str
    prompt_hash: str
    transcript_path: str
    code_path: str | None
    sample_index: int
    budget: Budget | None = None
    agent_outcome: AgentOutcome | None = None
    auto_submitted: bool = False
    turns_used: int | None = None
    wall_s: float | None = None
    generation: dict[str, object] = field(default_factory=dict)
    failure_kind: FailureKind | None = None
    failed_submits: int = 0


@dataclass
class TestResult:
    attempt_id: str
    test_id: str
    subtask: str
    verdict: TestVerdict
    cpu_s: float | None
    wall_s: float | None
    peak_memory_mb: float | None
    over_time: bool
    over_memory: bool
    score: float | None = None


@dataclass
class Verdict:
    attempt_id: str
    problem_id: str
    subtask_pass: dict[str, bool]
    all_pass: bool
    partial_score: float
    outcome: Outcome3
    env_id: str


def to_json(record: object) -> dict[str, object]:
    return asdict(record)

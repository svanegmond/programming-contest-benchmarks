import hashlib
import itertools
import json
import random
import zipfile
from pathlib import Path

import pytest

import admit
from cco_eval.waterloo import GradingKind
from judge import core
from judge.checkers import load_checker, run_checker

PROBLEMS = Path(__file__).resolve().parents[3] / "problems"


def check(problem_id: str, input_text: str, expected: str, output: str) -> dict:
    return load_checker(PROBLEMS / problem_id).check(input_text, expected, output)


SHIFTY_SAMPLE_1 = "2 4\n4 2 3 0\n1 5 6 7\n"


def test_shifty_accepts_any_solving_sequence_not_just_the_official_one():
    solved_by_other_route = "3\n2 1 1\n1 1 1\n1 2 0\n"
    assert check("cco-2017-d2p3", SHIFTY_SAMPLE_1, "2\n2 1 1\n1 1 1\n", solved_by_other_route)["ok"]


def test_shifty_accepts_zero_moves_on_a_solved_grid_as_official_data_does():
    assert check("cco-2017-d2p3", "2 2\n0 1\n2 3\n", "0\n", "0\n")["ok"]


@pytest.mark.parametrize(
    "output",
    [
        "1\n2 1 1\n",
        "2\n2 1 1\n1 1 1\n1 1 1\n",
        "2\n2 1 1\n1 3 1\n",
        "2\n2 1 1\n1 1 4\n",
        "2\n2 1 1\n",
        "",
    ],
)
def test_shifty_rejects_unsolved_malformed_or_out_of_range_moves(output):
    assert not check("cco-2017-d2p3", SHIFTY_SAMPLE_1, "2\n2 1 1\n1 1 1\n", output)["ok"]


RAINY_INPUT = "3\n10 15 10\n20 20\n0 11\n"
RAINY_EXPECTED = "YES\n5\n10 0 10\n5 5 10\n"


def test_rainy_markets_accepts_a_different_valid_split_with_the_minimum_cost():
    two_shelters = "2\n5 5\n4\n0\n"
    assert check("cco-2022-d1p2", two_shelters, "YES\n0\n4 0 0\n", "YES\n0\n2 0 2\n")["ok"]
    assert check("cco-2022-d1p2", RAINY_INPUT, RAINY_EXPECTED, RAINY_EXPECTED)["ok"]
    assert not check("cco-2022-d1p2", RAINY_INPUT, RAINY_EXPECTED, "YES\n5\n9 0 11\n5 5 10\n")["ok"]


@pytest.mark.parametrize(
    "output",
    [
        "NO\n",
        "YES\n6\n10 0 10\n4 6 10\n",
        "YES\n5\n10 0 10\n5 5 11\n",
        "YES\n5\n10 0 10\n5 5\n",
        "YES\n5\n10 0 10\n15 5 0\n",
        "YES\n5\n10 0 10\n5 5 10\nextra\n",
        "yes\n5\n10 0 10\n5 5 10\n",
    ],
)
def test_rainy_markets_rejects_wrong_cost_overfull_shelters_or_stock(output):
    assert not check("cco-2022-d1p2", RAINY_INPUT, RAINY_EXPECTED, output)["ok"]


def test_rainy_markets_no_must_match_the_official_no():
    sample = "3\n10 15 10\n20 20\n0 0\n"
    assert check("cco-2022-d1p2", sample, "NO\n", "NO\n")["ok"]
    assert not check("cco-2022-d1p2", sample, "NO\n", "YES\n0\n10 0 10\n10 0 10\n")["ok"]


def test_rainy_markets_matches_exhaustive_tiny_assignments():
    rng = random.Random(20261020)
    for _ in range(40):
        n = rng.randrange(2, 5)
        shelters = [rng.randrange(4) for _ in range(n)]
        people = [rng.randrange(4) for _ in range(n - 1)]
        umbrellas = [rng.randrange(4) for _ in range(n - 1)]
        options = [[(x, y, p - x - y) for x in range(p + 1)
                    for y in range(min(stock, p - x) + 1)] for p, stock in zip(people, umbrellas)]
        feasible = []
        for splits in itertools.product(*options):
            loads = [0] * n
            for market, (x, y, z) in enumerate(splits):
                loads[market] += x
                loads[market + 1] += z
            if all(load <= capacity for load, capacity in zip(loads, shelters)):
                feasible.append((sum(y for x, y, z in splits), splits))
        input_text = "\n".join([str(n), *(" ".join(map(str, values)) for values in (shelters, people, umbrellas))])
        if not feasible:
            assert check("cco-2022-d1p2", input_text, "NO\n", "NO\n")["ok"]
            continue
        minimum = min(cost for cost, splits in feasible)

        def output(cost, splits):
            return f"YES\n{cost}\n" + "\n".join(" ".join(map(str, split)) for split in splits)

        expected = output(*next(item for item in feasible if item[0] == minimum))
        for cost, splits in feasible:
            assert check("cco-2022-d1p2", input_text, expected, output(cost, splits))["ok"] == (cost == minimum)


PIZZA_INPUT = "7\n1 2 3 2 2 1 3\n2 3 1 2 3 2 1\n"
PIZZA_EXPECTED = "2\n1 2 1 2 1 2 2\n1 2 2 2 1 2 1\n"


def test_pizza_party_accepts_the_official_arrangement():
    assert check("cco-2024-d1p2", PIZZA_INPUT, PIZZA_EXPECTED, PIZZA_EXPECTED)["ok"]


@pytest.mark.parametrize(
    "output",
    [
        "3\n1 2 1 2 1 2 2\n1 2 2 2 1 2 1\n",
        "2\n1 2 1 2 1 2 2\n1 2 2 2 1 2 2\n",
        "2\n1 2 1 2 1 2 2\n1 2 2 2 1 2\n",
        "2\n1 2 1 2 1 2 3\n1 2 2 2 1 2 1\n",
        "-1\n",
        "",
    ],
)
def test_pizza_party_rejects_wrong_k_empty_stack_or_wrong_flavour(output):
    assert not check("cco-2024-d1p2", PIZZA_INPUT, PIZZA_EXPECTED, output)["ok"]


def test_pizza_party_impossible_case_needs_minus_one():
    sample = "2\n1 2\n1 1\n"
    assert check("cco-2024-d1p2", sample, "-1\n", "-1\n")["ok"]
    assert not check("cco-2024-d1p2", sample, "-1\n", "1\n1 1\n1 1\n")["ok"]


SALESPERSON_INPUT = "4\nR\nRR\nBRB\n"
SALESPERSON_EXPECTED = "4\n1 2 3 4\n4\n2 1 4 3\n4\n3 4 1 2\n4\n4 2 1 3\n"
STATEMENT_SAMPLE_OUTPUT = "5\n1 4 2 1 3\n6\n2 3 1 2 3 4\n5\n3 1 2 3 4\n4\n4 3 1 2\n"


def test_salesperson_official_output_scores_full_marks():
    result = check("cco-2020-d2p1", SALESPERSON_INPUT, SALESPERSON_EXPECTED, SALESPERSON_EXPECTED)
    assert result["ok"] and result["score"] == 1.0


def test_salesperson_does_not_relax_route_lengths_for_malformed_official_samples():
    malformed = "1\n1 2\n1\n2 1\n"
    result = check("cco-2020-d2p1", "2\nB\n", malformed, malformed)
    assert result == {"ok": False, "score": 0.0, "message": "plan 1: missing or invalid length"}


def test_salesperson_statement_sample_scores_the_hand_computed_minimum():
    # K=4 everywhere: M=5 -> floor(8+8*3/3)=16, M=6 -> floor(8+8*2/3)=13, M=4 -> 25; minimum 13 of 25.
    result = check("cco-2020-d2p1", SALESPERSON_INPUT, SALESPERSON_EXPECTED, STATEMENT_SAMPLE_OUTPUT)
    assert result["score"] == pytest.approx(13 / 25)
    assert not result["ok"]


def test_salesperson_route_over_twice_optimal_scores_zero():
    output = "9\n1 2 1 2 1 2 1 2 3\n" + SALESPERSON_EXPECTED.split("\n", 2)[2]
    result = check("cco-2020-d2p1", SALESPERSON_INPUT, SALESPERSON_EXPECTED, output)
    assert result["score"] == 0.0


@pytest.mark.parametrize(
    "first_plan",
    [
        "4\n1 2 3 3\n",
        "4\n1 2 3 1\n",
        "4\n2 1 3 4\n",
        "3\n1 2 3\n",
        "4\n1 2 3 5\n",
    ],
)
def test_salesperson_invalid_plan_zeroes_the_whole_test(first_plan):
    output = first_plan + SALESPERSON_EXPECTED.split("\n", 2)[2]
    assert check("cco-2020-d2p1", SALESPERSON_INPUT, SALESPERSON_EXPECTED, output)["score"] == 0.0


def test_salesperson_second_ticket_is_invalid():
    colours = "3\nR\nBR\n"
    expected = "3\n1 2 3\n3\n2 1 3\n3\n3 1 2\n"
    # Route 1 2 1 3 2 3: edges 1-2 R, 2-1 R, 1-3 B, 3-2 R switches twice.
    output = "6\n1 2 1 3 2 3\n3\n2 1 3\n3\n3 1 2\n"
    assert check("cco-2020-d2p1", colours, expected, output)["score"] == 0.0


def test_run_checker_turns_crashes_and_bad_bytes_into_wrong_answers():
    checker = load_checker(PROBLEMS / "cco-2017-d2p3")
    crashed = run_checker(checker, b"not a grid", b"", b"0\n")
    assert not crashed.ok and crashed.message.startswith("checker_error")
    assert not run_checker(checker, SHIFTY_SAMPLE_1.encode(), b"", b"\xff\xfe").ok


def test_load_checker_requires_the_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_checker(tmp_path)


def _grade_setup(tmp_path, monkeypatch, kind, stdout_for_stdin):
    class Runner:
        def prepare(self, lang, source, workdir):
            artifact = workdir / "compiled"
            artifact.write_bytes(source)
            return {"ok": True, "artifact": str(artifact)}

        def run_artifact(self, lang, artifact, stdin, time_limit_s, kill_factor):
            return {
                "stdout": stdout_for_stdin(stdin) if callable(stdout_for_stdin) else stdout_for_stdin,
                "stderr": b"",
                "returncode": 0,
                "cpu_s": 0.01,
                "wall_s": 0.02,
                "peak_memory_mb": 1.0,
                "killed": False,
                "over_time": False,
                "meta": {},
            }

        def release(self, artifact):
            pass

    problem_dir = tmp_path / "problem"
    problem_dir.mkdir()
    (problem_dir / "problem.json").write_text(json.dumps({
        "id": "cco-test",
        "time_limit_s": 1,
        "memory_limit_mb": None,
        "grading_kind": kind,
        "subtasks": [{"name": "main", "weight": 1.0}],
    }))
    (tmp_path / "in.txt").write_text(SALESPERSON_INPUT)
    (tmp_path / "out.txt").write_text(SALESPERSON_EXPECTED)
    (problem_dir / "tests.jsonl").write_text(json.dumps({
        "problem_id": "cco-test", "subtask": "main", "input_path": "in.txt", "expected_path": "out.txt",
    }) + "\n")
    code = tmp_path / "submission.py"
    code.write_text("pass\n")
    monkeypatch.setattr(core, "_make_runner", lambda backend: Runner())
    monkeypatch.setattr(core, "env_id", lambda backend="host": "test-env")
    return problem_dir, code


def _grade(tmp_path, problem_dir, code):
    return core.grade("host", code, "python", problem_dir / "problem.json", problem_dir / "tests.jsonl", tmp_path, "attempt-1", tmp_path / "out")


def test_grade_scorer_reports_partial_score_and_wrong_answer_verdict(tmp_path, monkeypatch):
    problem_dir, code = _grade_setup(tmp_path, monkeypatch, "scorer", STATEMENT_SAMPLE_OUTPUT.encode())
    (problem_dir / "checker.py").write_bytes((PROBLEMS / "cco-2020-d2p1/checker.py").read_bytes())

    result = _grade(tmp_path, problem_dir, code)

    assert result["verdict"]["partial_score"] == pytest.approx(13 / 25)
    assert result["verdict"]["all_pass"] is False
    assert result["verdict"]["outcome"] == "fail"
    test_result = json.loads((tmp_path / "out/test_results.jsonl").read_text())
    assert test_result["verdict"] == "wrong_answer"
    assert test_result["score"] == pytest.approx(13 / 25)
    record = json.loads((tmp_path / "out/checker.json").read_text())
    assert record["runs_in"] == "host_python"
    assert record["checker_sha256"] == hashlib.sha256((problem_dir / "checker.py").read_bytes()).hexdigest()


def test_grade_validator_passes_output_that_differs_from_the_official_one(tmp_path, monkeypatch):
    problem_dir, code = _grade_setup(tmp_path, monkeypatch, "validator", SALESPERSON_EXPECTED.encode())
    (problem_dir / "checker.py").write_text(
        "def check(input_text, expected_text, output_text):\n"
        "    return {'ok': output_text.startswith('4'), 'score': 1.0, 'message': 'starts with 4'}\n"
    )
    (tmp_path / "out.txt").write_text("something else entirely\n")

    result = _grade(tmp_path, problem_dir, code)

    assert result["verdict"]["all_pass"] is True
    assert result["verdict"]["partial_score"] == 1.0
    assert json.loads((tmp_path / "out/test_results.jsonl").read_text())["verdict"] == "pass"


def test_grade_scorer_takes_the_minimum_across_tests(tmp_path, monkeypatch):
    outputs = iter([SALESPERSON_EXPECTED.encode(), STATEMENT_SAMPLE_OUTPUT.encode()])
    problem_dir, code = _grade_setup(tmp_path, monkeypatch, "scorer", lambda _: next(outputs))
    (problem_dir / "checker.py").write_bytes((PROBLEMS / "cco-2020-d2p1/checker.py").read_bytes())
    tests = problem_dir / "tests.jsonl"
    tests.write_text(tests.read_text() * 2)

    result = _grade(tmp_path, problem_dir, code)

    assert result["verdict"]["partial_score"] == pytest.approx(13 / 25)
    records = [json.loads(line) for line in (tmp_path / "out/test_results.jsonl").read_text().splitlines()]
    assert [record["score"] for record in records] == pytest.approx([1.0, 13 / 25])


def test_grade_scorer_runtime_error_scores_zero(tmp_path, monkeypatch):
    problem_dir, code = _grade_setup(tmp_path, monkeypatch, "scorer", SALESPERSON_EXPECTED.encode())
    (problem_dir / "checker.py").write_bytes((PROBLEMS / "cco-2020-d2p1/checker.py").read_bytes())
    runner = core._make_runner("host")
    run_artifact = runner.run_artifact

    def crashed(*args):
        return {**run_artifact(*args), "returncode": 1}

    monkeypatch.setattr(runner, "run_artifact", crashed)
    monkeypatch.setattr(core, "_make_runner", lambda _: runner)
    result = _grade(tmp_path, problem_dir, code)

    assert result["verdict"]["partial_score"] == 0.0
    record = json.loads((tmp_path / "out/test_results.jsonl").read_text())
    assert record["verdict"] == "runtime_error"
    assert record["score"] == 0.0


def test_grade_validator_without_checker_file_fails_fast(tmp_path, monkeypatch):
    problem_dir, code = _grade_setup(tmp_path, monkeypatch, "validator", b"x")
    with pytest.raises(FileNotFoundError):
        _grade(tmp_path, problem_dir, code)


def proof_summary(problem_id: str, conclusion: str = "admitted", **overrides: str) -> str:
    lines = {
        "official": "| Official outputs accepted | 4 of 4 |",
        "official_rejected": "| Official outputs rejected | 0 |",
        **{kind: f"| Mutation `{kind}` rejected | 3 of 3 tried (accepted 0, crashes 0) |" for kind in admit.PROOF_MUTATIONS},
    }
    lines.update(overrides)
    return f"# Checker proof summary: {problem_id}\n\nConclusion: {conclusion}\n\n" + "\n".join(lines.values()) + "\n"


def test_admission_reads_the_proof_summary_when_present_and_tolerates_it_missing(tmp_path):
    row = {"checker_class": "any-valid", "io_mode": "stdin/stdout"}
    assert admit._status_reason(row)[1] is not None
    assert not admit.checker_proven(None)
    assert not admit.checker_proven(tmp_path)

    (tmp_path / "checker.py").write_text("def check(a, b, c): ...\n")
    assert not admit.checker_proven(tmp_path)

    (tmp_path / admit.CHECKER_PROOF).write_text(proof_summary(tmp_path.name))
    assert admit.checker_proven(tmp_path)
    assert admit._status_reason(row, True) == (GradingKind.VALIDATOR, None)
    assert admit._status_reason({"checker_class": "scorer", "io_mode": "stdin/stdout"}, True) == (GradingKind.SCORER, None)
    assert admit._status_reason({"checker_class": "scorer", "io_mode": "output-only"}, True)[1] is not None

    (tmp_path / admit.CHECKER_PROOF).unlink()
    assert not admit.checker_proven(tmp_path)
    assert admit._status_reason(row, admit.checker_proven(tmp_path))[1] is not None

    (tmp_path / admit.CHECKER_PROOF).write_bytes(b"\xff\xfe not text")
    assert not admit.checker_proven(tmp_path)


@pytest.mark.parametrize("defect", ["rejected_conclusion", "official_rejected", "too_few_mutations", "mutant_accepted", "checker_crash", "missing_evidence"])
def test_admission_rejects_proof_summaries_that_do_not_meet_the_rule(tmp_path, defect):
    (tmp_path / "checker.py").write_text("def check(a, b, c): ...\n")
    kwargs: dict[str, str] = {}
    conclusion = "admitted"
    if defect == "rejected_conclusion":
        conclusion = "rejected"
    elif defect == "official_rejected":
        kwargs["official"] = "| Official outputs accepted | 3 of 4 |"
    elif defect == "too_few_mutations":
        kwargs["change_one_value"] = "| Mutation `change_one_value` rejected | 2 of 2 tried (accepted 0, crashes 0) |"
    elif defect == "mutant_accepted":
        kwargs["change_one_value"] = "| Mutation `change_one_value` rejected | 3 of 4 tried (accepted 1, crashes 0) |"
    elif defect == "checker_crash":
        kwargs["violate_constraint"] = "| Mutation `violate_constraint` rejected | 3 of 3 tried (accepted 0, crashes 1) |"
    else:
        kwargs["violate_constraint"] = ""
    (tmp_path / admit.CHECKER_PROOF).write_text(proof_summary(tmp_path.name, conclusion, **kwargs))
    assert not admit.checker_proven(tmp_path)


def test_committed_proof_summaries_match_the_recorded_admissions():
    admitted = {"cco-2017-d2p3", "cco-2022-d1p2", "cco-2024-d1p2"}
    for problem_id in admitted | {"cco-2020-d2p1"}:
        problem_dir = PROBLEMS / problem_id
        status = json.loads((problem_dir / "status.json").read_text())["status"]
        assert admit.checker_proven(problem_dir) is (problem_id in admitted)
        assert (status == "admitted") is (problem_id in admitted)


def test_sample_match_on_input_only_ignores_an_example_output_that_differs():
    samples = [("2 2\n0 1\n2 3", "1\n1 1 0")]
    pairs = [{"input_name": "t.in", "expected_name": "t.a", "input_text": "2 2\n0 1\n2 3\n", "expected_text": "0\n"}]
    assert admit.sample_matches(samples, pairs) is None
    assert admit.sample_matches(samples, pairs, input_only=True) == "t.in"

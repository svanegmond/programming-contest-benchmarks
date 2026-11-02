import json
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jev_calibration
import jev_judge
from jev_judge import Cause, HttpJevClient, JevResponseError, MissingKeyError


def answer(p_valid=0.9, cause="valid", model="jev-1.14.0", input_tokens=100):
    return {
        "model": model,
        "answers": {
            "p_valid": {"type": "noul", "noul": p_valid},
            "cause": {"type": "choice", "choice": cause, "confidence": 0.8, "probabilities": {cause: 0.8}},
        },
        "usage": {"input_tokens": input_tokens, "output_tokens": 4},
    }


class FakeJev:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []

    def system_one(self, *, model, state, questions):
        self.calls.append({"model": model, "state": state, "questions": questions})
        response = self.responses.pop(0) if self.responses else answer()
        if isinstance(response, Exception):
            raise response
        return response


def case(test_id="t1", output="YES\n", attempt_id="a1", problem_id="p1"):
    return {"attempt_id": attempt_id, "test_id": test_id, "problem_id": problem_id,
            "input": "3\n", "expected": "YES\n", "output": output}


@pytest.fixture
def problems_root(tmp_path):
    folder = tmp_path / "problems" / "p1"
    folder.mkdir(parents=True)
    (folder / "problem.json").write_text(json.dumps({"id": "p1", "year": 2022, "day": 1, "statement": "Print YES."}))
    return tmp_path / "problems"


def test_row_has_the_required_fields_and_raw_keeps_the_response(problems_root, tmp_path):
    out = tmp_path / "jev_results.jsonl"
    jev_judge.judge_cases(FakeJev([answer(0.12, "wrong_format")]), [case()], out, problems_root=problems_root)
    row = json.loads(out.read_text())
    assert {k: row[k] for k in ("attempt_id", "test_id", "p_valid", "cause", "jev_model")} == {
        "attempt_id": "a1", "test_id": "t1", "p_valid": 0.12, "cause": "wrong_format", "jev_model": "jev-1.14.0"}
    assert row["raw"]["usage"]["input_tokens"] == 100
    assert row["raw"]["latency_s"] >= 0


def test_request_sends_statement_input_reference_and_output_with_both_question_types(problems_root, tmp_path):
    fake = FakeJev()
    jev_judge.judge_cases(fake, [case(output="NO\n")], tmp_path / "r.jsonl", problems_root=problems_root, model="jev-1.13.0")
    call = fake.calls[0]
    assert call["model"] == "jev-1.13.0"
    assert call["state"] == {"problem_statement": "Print YES.", "test_input": "3\n", "reference_output": "YES\n", "model_output": "NO\n"}
    assert call["questions"]["p_valid"]["type"] == "noul"
    choice = call["questions"]["cause"]
    assert choice["type"] == "choice" and set(choice["criteria"]) == {c.value for c in Cause}


def test_rerun_skips_finished_cases_and_retries_errors(problems_root, tmp_path):
    out = tmp_path / "r.jsonl"
    cases = [case("t1"), case("t2")]
    first = FakeJev([answer(), JevResponseError("HTTP 400: max_tokens_exceeded")])
    results = jev_judge.judge_cases(first, cases, out, problems_root=problems_root, workers=1)
    assert [r.p_valid is None for r in results] == [False, True]
    assert "max_tokens_exceeded" in results[1].raw["error"]

    second = FakeJev([answer(0.3, "suboptimal")])
    jev_judge.judge_cases(second, cases, out, problems_root=problems_root, workers=1)
    assert len(second.calls) == 1
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [(r["test_id"], r["p_valid"]) for r in rows] == [("t1", 0.9), ("t2", None), ("t2", 0.3)]


def test_oversized_state_is_an_error_row_without_calling_jev(problems_root, tmp_path):
    fake = FakeJev()
    big = case(output="9 " * 50_000)
    results = jev_judge.judge_cases(fake, [big], tmp_path / "r.jsonl", problems_root=problems_root)
    assert fake.calls == []
    assert results[0].p_valid is None and "state_too_large" in results[0].raw["error"]


@pytest.mark.parametrize("response", [
    {"model": "m", "answers": {"p_valid": {"noul": 1.4}, "cause": {"choice": "valid"}}},
    {"model": "m", "answers": {"p_valid": {"noul": 0.5}, "cause": {"choice": "banana"}}},
    {"model": "m", "answers": {}},
])
def test_malformed_response_becomes_an_error_row(problems_root, tmp_path, response):
    results = jev_judge.judge_cases(FakeJev([response]), [case()], tmp_path / "r.jsonl", problems_root=problems_root)
    assert results[0].p_valid is None and results[0].cause is None
    assert "error" in results[0].raw


def test_case_missing_fields_fails_fast(problems_root, tmp_path):
    with pytest.raises(ValueError, match="output"):
        jev_judge.judge_cases(FakeJev(), [{k: v for k, v in case().items() if k != "output"}], tmp_path / "r.jsonl", problems_root=problems_root)


def test_missing_api_key_exits_with_a_clear_message(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(MissingKeyError, match="TYPESAFE_API_KEY"):
        HttpJevClient()
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps(case()) + "\n")
    assert jev_judge.main(["--cases", str(cases), "--out", str(tmp_path / "r.jsonl")]) == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "r.jsonl").exists()


def test_calibrate_without_key_exits_cleanly(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    (tmp_path / "cases.jsonl").write_text("")
    monkeypatch.setattr(jev_judge, "CALIBRATION_DIR", tmp_path)
    assert jev_judge.main(["--calibrate"]) == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


class _Server:
    def __init__(self, statuses):
        outer = self
        outer.requests, outer.statuses = [], list(statuses)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"auth": self.headers["Authorization"], "path": self.path, "body": body})
                status = outer.statuses.pop(0) if outer.statuses else 200
                payload = json.dumps(answer(0.77, "valid") if status == 200 else {"detail": "slow down"}).encode()
                self.send_response(status)
                if status == 429:
                    self.send_header("retry-after", "0")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/systemone"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_http_client_sends_bearer_key_and_retries_rate_limits(problems_root, tmp_path):
    server = _Server([429, 529, 200])
    try:
        client = HttpJevClient("secret", url=server.url, retries=3)
        results = jev_judge.judge_cases(client, [case()], tmp_path / "r.jsonl", problems_root=problems_root)
    finally:
        server.close()
    assert results[0].p_valid == 0.77
    assert len(server.requests) == 3
    assert server.requests[0]["auth"] == "Bearer secret"
    assert set(server.requests[0]["body"]) == {"model", "state", "questions"}


def test_http_client_gives_up_on_non_retryable_errors(problems_root, tmp_path):
    server = _Server([422])
    try:
        client = HttpJevClient("secret", url=server.url)
        results = jev_judge.judge_cases(client, [case()], tmp_path / "r.jsonl", problems_root=problems_root)
    finally:
        server.close()
    assert len(server.requests) == 1
    assert "HTTP 422" in results[0].raw["error"]


RAINY_INPUT = "3\n10 15 10\n20 20\n0 11\n"
RAINY_EXPECTED = "YES\n5\n10 0 10\n5 5 10\n"
PATROL_INPUT = "4\n0 0\n1 1\n1 2\n2 1\n"
PATROL_EXPECTED = "3\n1 2\n2 3\n2 4\n"


def test_rainy_markets_corruptions_are_invalid_by_construction():
    m = jev_calibration.rainy_markets_mutations(RAINY_INPUT, RAINY_EXPECTED)
    assert m["truncated"][0] == "YES\n5\n"
    assert m["value_changed"][0] == "YES\n6\n10 0 10\n5 5 10\n"
    rows = [[int(v) for v in line.split()] for line in m["constraint_violated"][0].split("\n")[2:] if line]
    assert rows[0][0] < 0 and sum(rows[0]) == 20
    assert all(verified for _, _, verified in m.values())


def test_patrol_corruptions_are_invalid_by_construction():
    m = jev_calibration.patrol_robot_mutations(PATROL_INPUT, PATROL_EXPECTED)
    changed = m["value_changed"][0].split("\n")
    edges = [tuple(map(int, line.split())) for line in changed[1:] if line]
    assert int(changed[0]) == len(edges) and not jev_calibration._connected(4, edges)
    assert m["constraint_violated"][0].splitlines()[-1] == "1 1"
    assert m["constraint_violated"][0].splitlines()[0] == "4"
    assert all(verified for _, _, verified in m.values())


def test_patrol_change_that_cannot_be_verified_is_marked_unverified():
    triangle = jev_calibration.patrol_robot_mutations("3\n0 0\n1 0\n0 1\n", "3\n1 2\n2 3\n1 3\n")
    assert triangle["value_changed"][2] is None


def test_prep_reads_zips_and_skips_tests_with_unverifiable_corruptions(tmp_path):
    zips = tmp_path / "zips"
    (zips / "2025").mkdir(parents=True)
    members = {"d2p2.1-01.in": "3\n0 0\n1 0\n0 1\n", "d2p2.1-01.out": "3\n1 2\n2 3\n1 3\n",
               "d2p2.1-02.in": PATROL_INPUT, "d2p2.1-02.out": PATROL_EXPECTED}
    with zipfile.ZipFile(zips / "2025/data.zip", "w") as z:
        for name, text in members.items():
            z.writestr(f"d2p2/{name}", text)
    tests = [{"problem_id": "cco-2025-d2p2", "subtask": "subtask_1",
              "input_path": f"2025/data.zip::d2p2/{n}.in", "expected_path": f"2025/data.zip::d2p2/{n}.out"}
             for n in ("d2p2.1-01", "d2p2.1-02")]
    cases = jev_calibration.build_cases("cco-2025-d2p2", tests, zips)
    assert {c["test_id"] for c in cases} == {"d2p2.1-02"}
    assert [c["variant"] for c in cases] == ["expected", "truncated", "value_changed", "constraint_violated"]
    assert cases[0]["output"] == PATROL_EXPECTED and cases[0]["ground_truth"] == "valid"
    assert all(c["ground_truth"] == "invalid" and c["ground_truth_verified"] for c in cases[1:])


def test_summary_reports_separation_cost_and_latency():
    cases = [
        {"attempt_id": "a-expected", "test_id": "t", "variant": "expected"},
        {"attempt_id": "a-truncated", "test_id": "t", "variant": "truncated"},
        {"attempt_id": "a-changed", "test_id": "t", "variant": "value_changed"},
    ]

    def row(attempt_id, p, cause, latency=0.5, error=False):
        raw = {"error": "x"} if error else {"usage": {"input_tokens": 1_000_000, "output_tokens": 1}, "latency_s": latency}
        return {"attempt_id": attempt_id, "test_id": "t", "p_valid": None if error else p, "cause": cause, "jev_model": "m", "raw": raw}

    results = [row("a-expected", 0.9, "valid"), row("a-truncated", 0.1, "wrong_format"),
               row("a-changed", None, None, error=True), row("a-changed", 0.7, "valid", latency=1.5)]
    summary = jev_calibration.summarize(cases, results)
    assert summary["separation_at_0.5"] == {"valid_accepted": 1, "valid_total": 1, "invalid_rejected": 1, "invalid_total": 2}
    assert summary["variants"]["truncated"]["causes"] == {"wrong_format": 1}
    assert summary["errors"] == 0
    assert summary["cost_usd"] == pytest.approx(0.126)
    assert summary["latency_s"]["max"] == 1.5


@pytest.mark.skipif(not (jev_judge.CALIBRATION_DIR / "cases.jsonl").exists(),
                    reason="cases.jsonl is built from the official ZIPs: run jev_calibration.py prep")
def test_calibrate_runs_end_to_end_on_the_prepared_cases(monkeypatch, tmp_path):
    cases_path = jev_judge.CALIBRATION_DIR / "cases.jsonl"
    cases = jev_judge.read_jsonl(cases_path)
    assert len(cases) == 24
    fake = FakeJev([answer(0.95 if c["variant"] == "expected" else 0.05, "valid" if c["variant"] == "expected" else "wrong_format")
                    for c in cases])
    monkeypatch.setattr(jev_judge, "HttpJevClient", lambda: fake)
    (tmp_path / "cases.jsonl").write_bytes(cases_path.read_bytes())
    monkeypatch.setattr(jev_judge, "CALIBRATION_DIR", tmp_path)
    assert jev_judge.main(["--calibrate", "--workers", "1"]) == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["separation_at_0.5"] == {"valid_accepted": 6, "valid_total": 6, "invalid_rejected": 18, "invalid_total": 18}
    statements = {c["problem_id"] for c in cases}
    assert all("Time Limit" in call["state"]["problem_statement"] for call in fake.calls) and len(statements) == 2


def test_parser_accepts_a_captured_live_jev_response():
    live = json.loads((Path(__file__).parent / "fixtures/jev_live_response.json").read_text())
    p_valid, cause, model = jev_judge.parse_response(live)
    assert (p_valid, cause, model) == (0.98, Cause.VALID, "jev-1.14.0")

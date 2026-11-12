from __future__ import annotations

import json
from dataclasses import replace

import pytest

from cco_eval import batch as batch_api
from cco_eval.utils import write_json
from runners.common import ProblemData
from runners.judge_client import StubJudge
from waterloo import cli

BASE = "openrouter/acme/coder"
MODEL = BASE + ":batch"
CATALOG = {
    "acme/coder": {"id": "acme/coder", "pricing": {"prompt": "0.000002", "completion": "0.00001"},
                   "supported_parameters": ["reasoning", "temperature", "max_tokens"],
                   "reasoning": {"supported_efforts": ["high", "low"]}},
    "acme/coder:batch": {"id": "acme/coder:batch", "pricing": {"prompt": "0.000001", "completion": "0.000005"},
                         "supported_parameters": ["reasoning", "temperature", "max_tokens"],
                         "reasoning": {"supported_efforts": ["high", "low"]}},
}
PROGRAM = "```python\nimport sys\nprint(sum(map(int, sys.stdin.read().split())))\n```"


def chat_body(text=PROGRAM, cost=0.01, gen_id="gen-1"):
    return {"id": gen_id, "model": "acme/coder", "provider": "Acme",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": cost}}


class FakeHTTP:
    """Scripted OpenRouter: answers GET /credits, records POST /batches, replays poll payloads."""

    def __init__(self, polls, credits=(10.0, 1.0), submit=(202, None)):
        self.polls = list(polls)
        self.credits = credits
        self.submit = submit
        self.calls = []
        self.submitted = None

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/credits":
            total, used = self.credits
            return 200, {"data": {"total_credits": total, "total_usage": used}}
        if method == "POST" and path == "/batches":
            self.submitted = body
            status, payload = self.submit
            return status, payload or {"id": "batch_1", "status": "validating", "created_at": 1000,
                                       "provider": "Acme"}
        if path.startswith("/batches/"):
            return self.polls.pop(0)
        if path.startswith("/generation"):
            return 200, {"data": {"total_cost": 0.5}}
        raise AssertionError(path)


def completed(results, usage=None):
    return 200, {"id": "batch_1", "status": "completed", "finalized_at": 1090, "provider": "Acme",
                 "request_counts": {"total": len(results), "completed": len(results), "failed": 0},
                 "usage": usage, "results": results}


def result(cid, body=None, error=None):
    item = {"custom_id": cid, "id": "r-" + cid}
    if error:
        item["error"] = {"message": error}
    else:
        item["response"] = {"status_code": 200, "body": body or chat_body()}
    return item


def test_ids_and_pricing_lookup():
    assert batch_api.is_batch_model(MODEL) and not batch_api.is_batch_model(BASE)
    assert batch_api.as_batch_model(BASE) == MODEL == batch_api.as_batch_model(MODEL)
    assert batch_api.submit_slug(MODEL) == "acme/coder"
    # Batch price, not the sync price: 100 * 1e-6 + 1000 * 5e-6
    assert batch_api.worst_case_cost(MODEL, CATALOG, 100, 1000) == pytest.approx(0.0051)
    with pytest.raises(ValueError, match="no catalog pricing"):
        batch_api.worst_case_cost("openrouter/acme/unknown:batch", CATALOG, 1, 1)


def test_guard_refuses_when_credits_below_worst_case():
    http = FakeHTTP([], credits=(1.0, 0.9))
    with pytest.raises(RuntimeError, match="below the batch worst case"):
        batch_api.ensure_affordable(http, 0.2)
    assert batch_api.ensure_affordable(FakeHTTP([], credits=(1.0, 0.5)), 0.2) == pytest.approx(0.5)


def test_submit_writes_job_id_then_resume_does_not_resubmit(tmp_path):
    state_path = tmp_path / "batches" / "m.active.json"
    http = FakeHTTP([])
    requests = [batch_api.BatchRequest("a", {"messages": []})]
    state = batch_api.submit_or_resume(MODEL, requests, state_path, http, log=lambda _: None)
    assert state["batch_id"] == "batch_1"
    assert list(http.submitted) == ["endpoint", "model", "completion_window", "requests"]
    assert http.submitted["model"] == "acme/coder"
    again = FakeHTTP([])
    resumed = batch_api.submit_or_resume(MODEL, requests, state_path, again, log=lambda _: None)
    assert resumed["batch_id"] == "batch_1"
    assert again.calls == []


def test_submit_error_is_not_retried(tmp_path):
    http = FakeHTTP([], submit=(402, {"error": {"message": "Insufficient credits"}}))
    with pytest.raises(batch_api.BatchHTTPError, match="402"):
        batch_api.submit_or_resume(MODEL, [batch_api.BatchRequest("a", {})], tmp_path / "s.json", http,
                                   log=lambda _: None)
    assert http.calls == [("POST", "/batches")]
    assert not (tmp_path / "s.json").exists()


def test_poll_backs_off_through_429_and_5xx_without_a_deadline(tmp_path):
    state_path = tmp_path / "s.json"
    state = {"batch_id": "batch_1", "model": MODEL, "submitted_unix": 1000.0, "status": "validating"}
    polls = [(200, {"status": "in_progress"}), (429, {}), (503, {}),
             (200, {"status": "completed", "results": None}),  # final but results not attached yet
             completed([result("a")])]
    sleeps = []
    final = batch_api.poll(state, state_path, FakeHTTP(polls), sleep=sleeps.append, log=lambda _: None,
                           start_s=10, max_s=40)
    assert final["status"] == "completed"
    assert sleeps == [10, 120, 15, 30]
    saved = json.loads(state_path.read_text())
    assert saved["latency_s"] == 90.0 and saved["completed_at"].endswith("Z")


def test_poll_tolerates_404_right_after_submit_then_gives_up(tmp_path):
    state = {"batch_id": "batch_1", "model": MODEL, "submitted_unix": 1000.0}
    polls = [(404, {})] * 2 + [completed([result("a")])]
    final = batch_api.poll(state, tmp_path / "s.json", FakeHTTP(polls), sleep=lambda _: None, log=lambda _: None)
    assert final["status"] == "completed"
    missing = [(404, {"error": {"message": "not found"}})] * (batch_api.NOT_FOUND_TOLERANCE + 1)
    with pytest.raises(batch_api.BatchHTTPError, match="404"):
        batch_api.poll(state, tmp_path / "s.json", FakeHTTP(missing), sleep=lambda _: None, log=lambda _: None)


def test_poll_raises_on_terminal_http_errors(tmp_path):
    state = {"batch_id": "batch_1", "model": MODEL, "submitted_unix": 0}
    with pytest.raises(batch_api.BatchHTTPError, match="410"):
        batch_api.poll(state, tmp_path / "s.json", FakeHTTP([(410, {"error": {"message": "gone"}})]),
                       sleep=lambda _: None, log=lambda _: None)


def test_map_results_partial_failure_and_cost_sources():
    state = {"batch_id": "batch_1", "submit_model": "acme/coder", "latency_s": 90.0, "status": "completed",
             "custom_ids": ["a", "b", "c", "d"], "provider": "Acme"}
    no_cost = chat_body(gen_id="gen-9")
    no_cost["usage"].pop("cost")
    batch = completed([result("a"), result("b", error="overloaded"), result("c", body=no_cost)])[1]
    mapped = batch_api.map_results(batch, state, FakeHTTP([]))
    assert mapped["a"].text == PROGRAM
    assert mapped["a"].latency_ms == 90000.0
    assert mapped["a"].raw_provider_metadata["batch"]["batch_id"] == "batch_1"
    assert mapped["b"] == "overloaded"
    assert mapped["c"].raw_provider_metadata["usage"]["cost"] == 0.5
    assert mapped["c"].raw_provider_metadata["usage"]["cost_source"] == "generation"
    assert "without a result" in mapped["d"]


def test_single_request_batch_takes_batch_level_cost():
    body = chat_body()
    body["usage"].pop("cost")
    state = {"batch_id": "b", "submit_model": "acme/coder", "latency_s": 1, "custom_ids": ["a"]}
    _, batch = completed([result("a", body=body)], usage={"cost": 0.02, "is_byok": False})
    mapped = batch_api.map_results(batch, state)
    assert mapped["a"].raw_provider_metadata["usage"]["cost"] == 0.02


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    (tmp_path / "a.in").write_text("2 3\n")
    (tmp_path / "a.out").write_text("5\n")
    tests = tmp_path / "tests.jsonl"
    tests.write_text(json.dumps({"id": "t1", "input_path": "a.in", "expected_path": "a.out"}) + "\n")
    problem = ProblemData("p", 2024, 1, "Sum", "Print the sum.", None, 1, 128, {}, tests, None)
    monkeypatch.setattr(cli, "load_problem", lambda pid: replace(problem, id=pid))
    monkeypatch.setattr(cli, "admitted_problem_ids", lambda root: {"p", "q"})
    monkeypatch.setattr(cli, "RemoteJudge", lambda **kw: StubJudge())
    monkeypatch.setattr(cli, "load_openrouter_catalog", lambda **kw: CATALOG)
    monkeypatch.setattr("runners.single_shot.run_model",
                        lambda *a, **kw: pytest.fail("batch runs must not call the sync endpoint"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-sent")
    monkeypatch.setattr(batch_api.time, "sleep", lambda _: None)
    cohort = tmp_path / "cohort.json"
    write_json(cohort, {"seed": 1, "deterministic": ["p", "q"], "unusual": []})
    argv = ["run", "--models", BASE, "--batch", "--cohort", str(cohort), "--mode", "single_shot",
            "--lane", "python", "--max-tokens", "1000", "--out", str(tmp_path / "runs")]
    return tmp_path, argv


def use_http(monkeypatch, http):
    monkeypatch.setattr(batch_api, "OpenRouterHTTP", lambda **kw: http)


def test_cli_batch_run_one_job_per_model_records_batch_metadata(experiment, monkeypatch):
    root, argv = experiment
    http = FakeHTTP([completed([result("p--s1"), result("q--s1")])])
    use_http(monkeypatch, http)
    assert cli.main(argv) == 0
    assert [c for c in http.calls if c[0] == "POST"] == [("POST", "/batches")]
    assert [r["custom_id"] for r in http.submitted["requests"]] == ["p--s1", "q--s1"]
    assert http.submitted["requests"][0]["body"]["model"] == "acme/coder"
    run_dir = next((root / "runs").iterdir())
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["models"] == [MODEL]
    assert manifest["caps"]["batch_models"] == [MODEL]
    attempt = json.loads(next(run_dir.rglob("attempt.json")).read_text())
    assert attempt["generation"]["batch"]["batch_id"] == "batch_1"
    assert not (run_dir / "batches" / "openrouter-acme-coder-batch.active.json").exists()
    archived = json.loads((run_dir / "batches" / "openrouter-acme-coder-batch.batch_1.json").read_text())
    assert archived["latency_s"] == 90.0
    telemetry = [json.loads(line) for line in (run_dir / "telemetry.jsonl").read_text().splitlines()]
    assert [row["cost_usd"] for row in telemetry] == [0.01, 0.01]


def test_cli_partial_failure_scores_the_rest_and_resubmits_only_failures(experiment, monkeypatch, capsys):
    root, argv = experiment
    use_http(monkeypatch, FakeHTTP([completed([result("p--s1"), result("q--s1", error="overloaded")])]))
    assert cli.main(argv) == 1
    assert "1 batch request(s)" in capsys.readouterr().err
    retry = FakeHTTP([completed([result("q--s1")])])
    use_http(monkeypatch, retry)
    assert cli.main(argv) == 0
    assert [r["custom_id"] for r in retry.submitted["requests"]] == ["q--s1"]
    run_dir = next((root / "runs").iterdir())
    assert len([p for p in run_dir.rglob("verdict.json") if p.parent.name == "s1"]) == 2


def test_cli_interrupted_poll_resumes_same_job(experiment, monkeypatch):
    root, argv = experiment
    first = FakeHTTP([(410, {"error": {"message": "simulated interruption"}})])
    use_http(monkeypatch, first)
    assert cli.main(argv) == 1
    run_dir = next((root / "runs").iterdir())
    assert json.loads((run_dir / "batches" / "openrouter-acme-coder-batch.active.json").read_text())["batch_id"] == "batch_1"
    second = FakeHTTP([completed([result("p--s1"), result("q--s1")])])
    use_http(monkeypatch, second)
    assert cli.main(argv) == 0
    assert ("POST", "/batches") not in second.calls
    assert ("GET", "/credits") not in second.calls


def test_cli_guard_blocks_submit(experiment, monkeypatch, capsys):
    root, argv = experiment
    http = FakeHTTP([], credits=(1.0, 0.999))
    use_http(monkeypatch, http)
    assert cli.main(argv) == 1
    assert "below the batch worst case" in capsys.readouterr().err
    assert ("POST", "/batches") not in http.calls


def test_cli_rejects_batch_in_agentic_mode_and_unknown_variant(experiment, monkeypatch, capsys):
    root, argv = experiment
    agentic = [a if a != "single_shot" else "agentic" for a in argv]
    assert cli.main(agentic) == 1
    assert "only in single_shot" in capsys.readouterr().err
    unknown = [a if a != BASE else "openrouter/acme/other" for a in argv]
    assert cli.main(unknown) == 1
    assert "no OpenRouter batch variant" in capsys.readouterr().err


def test_cli_dry_run_reports_worst_case_without_network(experiment, monkeypatch, capsys):
    root, argv = experiment
    use_http(monkeypatch, None)
    assert cli.main(argv + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "batch dry-run model=openrouter/acme/coder:batch submit_as=acme/coder requests=2" in out


def test_publish_overview_includes_batch_jobs(experiment, monkeypatch):
    root, argv = experiment
    use_http(monkeypatch, FakeHTTP([completed([result("p--s1"), result("q--s1")])]))
    assert cli.main(argv) == 0
    run_dir = next((root / "runs").iterdir())
    site = root / "site"
    assert cli.main(["publish", run_dir.name, "--note", "BYOK note", "--site", str(site),
                     "--out", str(root / "runs")]) == 0
    index = (site / "index.html").read_text()
    assert "BYOK note" in index and ">batch<" in index and "1/1" in index
    config = json.loads((site / run_dir.name / "run-config.json").read_text())
    assert config["batch_models"] == [MODEL]
    assert config["batches"][0]["batch_id"] == "batch_1"
    assert config["git_sha"]


def test_batch_results_keep_reasoning_and_the_overview_links_it(experiment, monkeypatch):
    root, argv = experiment
    reasoning_body = chat_body()
    reasoning_body["choices"][0]["message"]["reasoning"] = "Sum the two integers. " * 10
    reasoning_body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 50}
    http = FakeHTTP([completed([result("p--s1", reasoning_body), result("q--s1")])])
    use_http(monkeypatch, http)
    assert cli.main(argv) == 0
    assert http.submitted["requests"][0]["body"]["reasoning"] == {"effort": "high", "exclude": False}
    run_dir = next((root / "runs").iterdir())
    kinds = {}
    for path in run_dir.rglob("attempt.json"):
        attempt = json.loads(path.read_text())
        kinds[attempt["problem_id"]] = attempt["reasoning"]["kind"]
        assert path.with_name("reasoning.json").is_file() and path.with_name("reasoning.md").is_file()
    assert kinds == {"p": "full", "q": "none"}
    telemetry = {row["problem_id"]: row for row in map(json.loads, (run_dir / "telemetry.jsonl").read_text().splitlines())}
    assert telemetry["p"]["reasoning_kind"] == "full" and telemetry["p"]["reasoning_tokens"] == 50
    site = root / "site"
    assert cli.main(["publish", run_dir.name, "--note", "n", "--site", str(site), "--out", str(root / "runs"),
                     "--with-reasoning"]) == 0
    index = (site / "index.html").read_text()
    assert "<th>Reasoning</th>" in index and f'href="{run_dir.name}/reasoning/p--acme-coder-batch--s1.md">full<' in index
    assert ">none<" in index
    bare = root / "bare"
    assert cli.main(["publish", run_dir.name, "--note", "n", "--site", str(bare), "--out", str(root / "runs")]) == 0
    bare_index = (bare / "index.html").read_text()
    assert "<th>Reasoning</th>" in bare_index
    assert ">withheld<" in bare_index and "Reasoning traces were withheld from this report." in bare_index
    assert "not recorded" not in bare_index
    assert not (bare / run_dir.name / "reasoning").exists()

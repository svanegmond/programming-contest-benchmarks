"""Reasoning traces: each response shape OpenRouter documents, saving, and publishing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cco_eval.models import GenerateResult
from cco_eval.pages import publish
from cco_eval.providers import openrouter_chat_payload, parse_openrouter_chat
from cco_eval.reasoning import (
    ReasoningTurn, excerpt, public_trace, redact, sensitive_literals, strip_code, write_trace,
)
from cco_eval.utils import write_json
from cco_eval.waterloo import Lane
from runners.judge_client import StubJudge
from runners.single_shot import run_one
from tests.test_waterloo_runners import ScriptedModel, run_script, setup_problem, tool

PROGRAM = "```python\nimport sys\nprint(sum(map(int, sys.stdin.read().split())))\n```"
LONG = "Each move empties one cell, so the game lasts at most twelve moves. " * 40


def body(message_extra: dict, reasoning_tokens: int | None) -> dict:
    usage = {"prompt_tokens": 10, "completion_tokens": 20 + (reasoning_tokens or 0), "cost": 0.001}
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return {"id": "gen-1", "model": "acme/coder", "provider": "Acme",
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": PROGRAM, **message_extra}}],
            "usage": usage}


# Shapes from https://openrouter.ai/docs/guides/best-practices/reasoning-tokens (Chat Completions).
KIMI = body({"reasoning": LONG}, reasoning_tokens=len(LONG) // 4)
TEXT_DETAILS = body({"reasoning": LONG, "reasoning_details": [
    {"type": "reasoning.text", "text": LONG, "signature": "sig-abc", "id": "r1",
     "format": "anthropic-claude-v1", "index": 0}]}, reasoning_tokens=len(LONG) // 4)
CLAUDE_SUMMARIZED = body({"reasoning": "Memoize over boards.", "reasoning_details": [
    {"type": "reasoning.text", "text": "Memoize over boards.", "signature": "sig-xyz",
     "format": "anthropic-claude-v1", "index": 0}]}, reasoning_tokens=4000)
SUMMARY_DETAILS = body({"reasoning_details": [
    {"type": "reasoning.summary", "summary": "Analyzed the game tree.", "format": "anthropic-claude-v1"}]},
    reasoning_tokens=900)
GEMINI_ENCRYPTED = body({"reasoning_details": [
    {"type": "reasoning.encrypted", "data": "eyJ" + "A" * 500, "format": "google-gemini-v1", "index": 0}]},
    reasoning_tokens=25768)
HIDDEN = body({}, reasoning_tokens=5810)
NO_REASONING = body({}, reasoning_tokens=None)


@pytest.mark.parametrize("response,kind", [
    (KIMI, "full"), (TEXT_DETAILS, "full"), (CLAUDE_SUMMARIZED, "summary"), (SUMMARY_DETAILS, "summary"),
    (GEMINI_ENCRYPTED, "encrypted"), (HIDDEN, "none"), (NO_REASONING, "none"),
])
def test_each_reasoning_shape_is_kept_and_classified(response, kind):
    generation = parse_openrouter_chat(response, "acme/coder", 1.0)
    assert generation.text == PROGRAM
    summary = ReasoningTurn.from_generation(generation).summary()
    assert summary["kind"] == kind
    billed = response["usage"].get("completion_tokens_details", {}).get("reasoning_tokens")
    assert summary["billed_reasoning_tokens"] == billed


def test_reasoning_content_alias_and_visible_text_order():
    turn = ReasoningTurn.from_message({"reasoning_content": "alias text"}, None)
    assert turn.visible_text() == "alias text"
    mixed = ReasoningTurn(details=[{"type": "reasoning.summary", "summary": "S"},
                                   {"type": "reasoning.encrypted", "data": "x"},
                                   {"type": "reasoning.text", "text": "T"}])
    assert mixed.visible_text() == "S\n\nT"
    assert mixed.encrypted_count == 1


def test_payload_always_asks_for_reasoning_back():
    payload = openrouter_chat_payload("acme/coder", [], 100, reasoning_effort="high")
    assert payload["reasoning"] == {"effort": "high", "exclude": False}
    assert "reasoning" not in openrouter_chat_payload("acme/coder", [], 100, reasoning_effort=None)


def test_write_trace_records_kind_counts_and_paths(tmp_path):
    turn = ReasoningTurn.from_message(KIMI["choices"][0]["message"], KIMI["usage"])
    summary = write_trace(tmp_path, [(1, turn)], model="m", mode="single_shot")
    raw = json.loads((tmp_path / "reasoning.json").read_text())
    assert raw["schema"] == "reasoning-trace/v1"
    assert raw["turns"][0]["reasoning"] == LONG
    assert summary["kind"] == "full" and summary["visible_chars"] == len(LONG)
    assert summary["billed_reasoning_tokens"] == len(LONG) // 4
    assert summary["trace_path"] == str(tmp_path / "reasoning.json")
    assert LONG.strip() in (tmp_path / "reasoning.md").read_text()


def test_single_shot_saves_trace_and_attempt_fields(tmp_path):
    problem = setup_problem(tmp_path)
    generation = parse_openrouter_chat(CLAUDE_SUMMARIZED, "acme/coder", 1.0)
    result = run_one(problem, "openrouter/acme/coder", 1, out=tmp_path / "out", run_id="r",
                     judge=StubJudge(), timeout=1, max_tokens=1000, lane=Lane.PYTHON, generation=generation)
    artifact = Path(result["artifact"])
    attempt = json.loads((artifact / "attempt.json").read_text())
    assert attempt["reasoning"]["kind"] == "summary"
    assert attempt["reasoning"]["billed_reasoning_tokens"] == 4000
    assert attempt["reasoning"]["visible_chars"] == len("Memoize over boards.")
    assert attempt["reasoning"]["trace_path"] == str(artifact / "reasoning.json")
    assert attempt["generation"]["reasoning"]["kind"] == "summary"
    raw = json.loads((artifact / "reasoning.json").read_text())
    assert raw["turns"][0]["reasoning_details"][0]["signature"] == "sig-xyz"


def test_single_shot_without_a_program_still_saves_the_trace(tmp_path):
    problem = setup_problem(tmp_path)
    response = body({"reasoning": LONG}, reasoning_tokens=len(LONG) // 4)
    response["choices"][0]["message"]["content"] = ""
    response["choices"][0]["finish_reason"] = "length"
    result = run_one(problem, "openrouter/acme/coder", 1, out=tmp_path / "out", run_id="r",
                     judge=StubJudge(), timeout=1, max_tokens=1000, lane=Lane.PYTHON,
                     generation=parse_openrouter_chat(response, "acme/coder", 1.0))
    attempt = json.loads((Path(result["artifact"]) / "attempt.json").read_text())
    assert attempt["failure_kind"] == "truncated"
    assert attempt["reasoning"]["kind"] == "full"


def _agent_turn(message: dict, reasoning_tokens: int) -> GenerateResult:
    response = body(message, reasoning_tokens)
    response["choices"][0]["message"]["content"] = ""
    return parse_openrouter_chat(response, "acme/coder", 1.0)


def test_agent_mode_saves_each_turns_reasoning(tmp_path):
    problem = setup_problem(tmp_path)
    write = tool("write_file", path="sol.py", content="print(sum(map(int, input().split())))\n")
    submit = tool("submit", lang="python", code_path="sol.py")
    turn1 = _agent_turn({"reasoning": "Plan: read two ints.", "tool_calls": write["tool_calls"]}, 6)
    turn1.raw_provider_metadata["tool_calls"] = write["tool_calls"]
    turn2 = _agent_turn({"reasoning_details": [{"type": "reasoning.encrypted", "data": "zzz"}]}, 50)
    turn2.raw_provider_metadata["tool_calls"] = submit["tool_calls"]
    result = run_script(tmp_path, problem, ScriptedModel([turn1, turn2]), run="agent")
    artifact = Path(result["artifact"])
    raw = json.loads((artifact / "reasoning.json").read_text())
    assert [t["turn"] for t in raw["turns"]] == [1, 2]
    assert [t["kind"] for t in raw["turns"]] == ["full", "encrypted"]
    attempt = json.loads((artifact / "attempt.json").read_text())
    assert attempt["reasoning"]["kind"] == "full" and attempt["reasoning"]["turns"] == 2
    assert attempt["reasoning"]["billed_reasoning_tokens"] == 56
    assert [c["reasoning"]["kind"] for c in attempt["generation"]["calls"]] == ["full", "encrypted"]
    md = (artifact / "reasoning.md").read_text()
    assert "## Turn 1" in md and "Plan: read two ints." in md and "Encrypted" in md


def test_redaction_scrubs_keys_hosts_and_env_literals():
    text = ("key sk-or-v1-" + "a" * 64 + " and ghp_" + "B" * 36 + " via judge.lan or 192.168.1.20, "
            "ssh stephen@judge-box.example.org, token=supersecretvalue1, custom my-private-box")
    literals = sensitive_literals({"MY_API_KEY": "literal-secret-123", "CCO_REDACT": "my-private-box"})
    cleaned, count = redact(text + " literal-secret-123", literals)
    for leaked in ("sk-or-v1", "ghp_", "judge.lan", "192.168.1.20", "judge-box", "supersecretvalue1",
                   "literal-secret-123", "my-private-box"):
        assert leaked not in cleaned
    assert count >= 8
    assert redact("dp[i][j] = max(dp[i-1][j], 10)", literals)[1] == 0


def test_strip_code_and_excerpt():
    stripped, blocks = strip_code("before\n```cpp\nint main(){}\nreturn 0;\n```\nafter")
    assert blocks == 1 and "int main" not in stripped and "2 lines" in stripped
    text = "A" * 600 + "M" * 5000 + "Z" * 600
    short = excerpt(text)
    assert short.startswith("A" * 600) and short.endswith("Z" * 600) and "5,000 characters omitted" in short
    assert excerpt("tiny") == "tiny"


def test_public_trace_hides_encrypted_blobs_and_signatures(tmp_path):
    turn = ReasoningTurn.from_message(GEMINI_ENCRYPTED["choices"][0]["message"], GEMINI_ENCRYPTED["usage"])
    write_trace(tmp_path, [(1, turn)], model="m", mode="single_shot")
    published, text = public_trace(json.loads((tmp_path / "reasoning.json").read_text()), [])
    data = published["turns"][0]["reasoning_details"][0]["data"]
    assert data.startswith("[encrypted: 503 characters")
    assert "AAAA" not in json.dumps(published)
    assert "Encrypted" in text


def _run_dir(tmp_path: Path, response: dict) -> Path:
    problem = setup_problem(tmp_path)
    run_one(problem, "openrouter/acme/coder", 1, out=tmp_path / "runs", run_id="r1", judge=StubJudge(),
            timeout=1, max_tokens=1000, lane=Lane.PYTHON,
            generation=parse_openrouter_chat(response, "acme/coder", 1.0))
    run = tmp_path / "runs" / "r1"
    write_json(run / "run.json", {"run_id": "r1", "git_sha": "a" * 40, "models": ["openrouter/acme/coder"],
                                  "mode": "single_shot", "k": 1, "caps": {}})
    return run


def test_publish_writes_excerpt_and_redacted_full_trace(tmp_path, monkeypatch):
    monkeypatch.setenv("CCO_REDACT", "judge-host-7")
    secret = "sk-or-v1-" + "f" * 64
    trace = ("Start: think about judge-host-7 and " + secret + ". " + LONG
             + "\n```cpp\nint main() { return 0; }\n```\nEnd of thinking.")
    run = _run_dir(tmp_path, body({"reasoning": trace}, reasoning_tokens=len(trace) // 4))
    site = tmp_path / "site"
    publish(run, site, include_reasoning=True)
    config = json.loads((site / "run-config.json").read_text())
    entry = config["reasoning_traces"][0]
    assert config["reasoning_published"] is True and entry["kind"] == "full"
    assert entry["redactions"] >= 2 and entry["code_blocks_omitted"] == 1
    full = (site / entry["md"]).read_text()
    page = (site / "index.html").read_text()
    for published in (full, page, (site / entry["json"]).read_text()):
        assert secret not in published and "judge-host-7" not in published and "int main" not in published
    assert "Start: think about" in page and "End of thinking." in page and "characters omitted" in page
    assert f'href="{entry["md"]}"' in page and "Full reasoning text" in page


def test_publish_labels_hidden_traces_and_can_leave_traces_out(tmp_path):
    run = _run_dir(tmp_path, HIDDEN)
    site = tmp_path / "site"
    publish(run, site, include_reasoning=True)
    assert "Hidden: 5,810 reasoning tokens were billed but none were returned" in (site / "index.html").read_text()
    bare = tmp_path / "bare"
    publish(run, bare)
    config = json.loads((bare / "run-config.json").read_text())
    assert config["reasoning_published"] is False and "reasoning_traces" not in config
    assert not (bare / "reasoning").exists()
    html = (bare / "index.html").read_text()
    summary = (bare / "summary.md").read_text()
    assert "Reasoning traces were withheld from this report." in html
    assert "Reasoning traces were withheld from this report." in summary
    assert "not recorded" not in html and "not recorded" not in summary
    assert "<h2>Reasoning traces</h2>" not in html


def test_publish_withholds_traces_by_default(tmp_path):
    run = _run_dir(tmp_path, KIMI)
    site = tmp_path / "site"
    publish(run, site)
    config = json.loads((site / "run-config.json").read_text())
    html = (site / "index.html").read_text()
    assert config["reasoning_published"] is False and "reasoning_traces" not in config
    assert not (site / "reasoning").exists()
    assert "Reasoning traces were withheld from this report." in html
    assert LONG not in html and "not recorded" not in html
    published = tmp_path / "published"
    publish(run, published, include_reasoning=True)
    assert json.loads((published / "run-config.json").read_text())["reasoning_published"] is True
    assert (published / "reasoning").is_dir()
    assert "Reasoning traces were withheld from this report." not in (published / "index.html").read_text()


def test_publish_cli_with_reasoning_and_no_reasoning_noop(tmp_path):
    from waterloo import cli
    run = _run_dir(tmp_path, KIMI)
    out = str(tmp_path / "runs")
    bare = tmp_path / "bare"
    assert cli.main(["publish", "r1", "--note", "n", "--site", str(bare), "--out", out]) == 0
    index = (bare / "index.html").read_text()
    assert ">withheld<" in index and "Reasoning traces were withheld from this report." in index
    assert LONG not in index and "not recorded" not in index
    assert not (bare / "r1" / "reasoning").exists()
    noop = tmp_path / "noop"
    assert cli.main(["publish", "r1", "--note", "n", "--site", str(noop), "--out", out, "--no-reasoning"]) == 0
    assert ">withheld<" in (noop / "index.html").read_text()
    assert not (noop / "r1" / "reasoning").exists()
    full = tmp_path / "full"
    assert cli.main(["publish", "r1", "--note", "n", "--site", str(full), "--out", out, "--with-reasoning"]) == 0
    index = (full / "index.html").read_text()
    assert f'href="r1/reasoning/' in index and ">full<" in index
    assert (full / "r1" / "reasoning").is_dir()
    assert "withheld" not in index


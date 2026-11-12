"""Model reasoning traces: pull them out of OpenRouter responses, save them per attempt,
and prepare redacted, excerpted copies for publishing.

OpenRouter returns reasoning on Chat Completions in two places on ``choices[0].message``
(https://openrouter.ai/docs/guides/best-practices/reasoning-tokens):

- ``reasoning``: a plain string (``reasoning_content`` is an alias).
- ``reasoning_details``: an array of ``reasoning.text`` (raw text, optional signature),
  ``reasoning.summary`` (condensed) and ``reasoning.encrypted`` (opaque ``data``) items.

The billed count is ``usage.completion_tokens_details.reasoning_tokens``. Visible text can be
far shorter than that count when the provider summarizes (Claude's default summarized thinking,
Gemini thought summaries), so ``kind`` is decided from the detail types first and then from the
visible-to-billed ratio.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "reasoning-trace/v1"
KINDS = ("full", "summary", "encrypted", "none")
# Rough English/code tokenizer ratio, used only for the *estimated* visible token count.
CHARS_PER_TOKEN = 4
# Visible text under this share of the billed reasoning tokens is treated as a summary.
SUMMARY_RATIO = 0.5
EXCERPT_CHARS = 600


@dataclass
class ReasoningTurn:
    """The reasoning returned with one model response."""

    reasoning: str | None = None
    details: list[dict[str, Any]] = field(default_factory=list)
    billed_tokens: int | None = None

    @classmethod
    def from_message(cls, message: dict[str, Any] | None, usage: dict[str, Any] | None) -> "ReasoningTurn":
        message = message if isinstance(message, dict) else {}
        raw = message.get("reasoning")
        if raw is None:
            raw = message.get("reasoning_content")
        details = message.get("reasoning_details")
        return cls(
            reasoning=str(raw) if raw not in (None, "") else None,
            details=[d for d in details if isinstance(d, dict)] if isinstance(details, list) else [],
            billed_tokens=billed_reasoning_tokens(usage),
        )

    @classmethod
    def from_generation(cls, generation: Any) -> "ReasoningTurn":
        """From a ``GenerateResult`` (sync or batch) or an agent-loop mock ``{"message", "generation"}``."""
        if isinstance(generation, dict):
            message = generation.get("message", generation)
            meta = (generation.get("generation") or {}).get("raw_provider_metadata") or {}
            return cls.from_message(message if isinstance(message, dict) else {}, meta.get("usage"))
        meta = getattr(generation, "raw_provider_metadata", None) or {}
        return cls(reasoning=getattr(generation, "reasoning", None),
                   details=list(getattr(generation, "reasoning_details", None) or []),
                   billed_tokens=billed_reasoning_tokens(meta.get("usage")))

    def _parts(self, kind: str) -> list[str]:
        key = "text" if kind == "reasoning.text" else "summary"
        return [str(d.get(key)) for d in self.details if d.get("type") == kind and d.get(key)]

    @property
    def encrypted_count(self) -> int:
        return sum(1 for d in self.details if d.get("type") == "reasoning.encrypted")

    def visible_text(self) -> str:
        """Readable reasoning in the order returned; falls back to the ``reasoning`` string."""
        parts = [str(d.get("text") if d.get("type") == "reasoning.text" else d.get("summary"))
                 for d in self.details
                 if (d.get("type") == "reasoning.text" and d.get("text"))
                 or (d.get("type") == "reasoning.summary" and d.get("summary"))]
        return "\n\n".join(parts) if parts else (self.reasoning or "")

    def classify(self) -> tuple[str, str]:
        """(kind, basis). Detail types win; then a low visible/billed ratio means a summary."""
        texts, summaries = self._parts("reasoning.text"), self._parts("reasoning.summary")
        if texts or (self.reasoning and not summaries):
            kind, basis = "full", "reasoning.text" if texts else "reasoning string"
        elif summaries:
            return "summary", "reasoning.summary"
        elif self.encrypted_count:
            return "encrypted", "only reasoning.encrypted items"
        else:
            billed = self.billed_tokens or 0
            return "none", (f"{billed} reasoning tokens billed, none returned" if billed
                            else "no reasoning returned")
        estimate = estimate_tokens(self.visible_text())
        if self.billed_tokens and estimate < SUMMARY_RATIO * self.billed_tokens:
            share = 100 * estimate / self.billed_tokens
            return "summary", (f"visible text is ~{share:.0f}% of the {self.billed_tokens} billed "
                               "reasoning tokens, so the provider likely condensed it")
        return kind, basis

    def summary(self) -> dict[str, Any]:
        kind, basis = self.classify()
        text = self.visible_text()
        return {"kind": kind, "kind_basis": basis, "visible_chars": len(text),
                "visible_tokens_est": estimate_tokens(text), "billed_reasoning_tokens": self.billed_tokens,
                "encrypted_items": self.encrypted_count, "detail_types": [d.get("type") for d in self.details]}


def billed_reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    details = (usage or {}).get("completion_tokens_details") if isinstance(usage, dict) else None
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return int(value) if isinstance(value, (int, float)) else None


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN) if text else 0


def aggregate_kind(kinds: Iterable[str]) -> str:
    present = set(kinds)
    return next((kind for kind in KINDS if kind in present), "none")


def write_trace(artifact: Path, turns: list[tuple[int, ReasoningTurn]], *, model: str, mode: str) -> dict[str, Any]:
    """Write ``reasoning.json`` (raw) and ``reasoning.md`` (readable); return the attempt summary."""
    records = []
    for number, turn in turns:
        records.append({"turn": number, **turn.summary(), "reasoning": turn.reasoning,
                        "reasoning_details": turn.details})
    billed = [r["billed_reasoning_tokens"] for r in records]
    summary: dict[str, Any] = {
        "kind": aggregate_kind(r["kind"] for r in records),
        "visible_chars": sum(r["visible_chars"] for r in records),
        "visible_tokens_est": sum(r["visible_tokens_est"] for r in records),
        "billed_reasoning_tokens": sum(billed) if billed and all(b is not None for b in billed) else None,
        "turns": len(records),
        "trace_path": str(artifact / "reasoning.json"),
        "text_path": str(artifact / "reasoning.md"),
    }
    if len(records) == 1:
        summary["kind_basis"] = records[0]["kind_basis"]
    payload = {"schema": SCHEMA, "model": model, "mode": mode,
               "summary": {k: v for k, v in summary.items() if not k.endswith("_path")}, "turns": records}
    (artifact / "reasoning.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (artifact / "reasoning.md").write_text(render_markdown(payload), encoding="utf-8")
    return summary


def kind_label(kind: str, billed: int | None = None) -> str:
    if kind == "full":
        return "Full reasoning text"
    if kind == "summary":
        return "Summary only: the provider returned a condensed version of its reasoning"
    if kind == "encrypted":
        return "Encrypted: the provider returned only an opaque blob, nothing readable"
    if billed:
        return f"Hidden: {billed:,} reasoning tokens were billed but none were returned"
    return "No reasoning returned"


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# Reasoning trace: {payload.get('model')} ({payload.get('mode')})", ""]
    many = len(payload.get("turns", [])) > 1
    for record in payload.get("turns", []):
        if many:
            lines += [f"## Turn {record['turn']}", ""]
        billed = record.get("billed_reasoning_tokens")
        lines += [f"_{kind_label(record['kind'], billed)}. {record['visible_chars']:,} visible characters "
                  f"(~{record['visible_tokens_est']:,} tokens est.) vs "
                  f"{'unknown' if billed is None else f'{billed:,}'} billed reasoning tokens._", ""]
        text = ReasoningTurn(record.get("reasoning"), record.get("reasoning_details") or []).visible_text()
        lines += [text if text else "(no readable reasoning)", ""]
    return "\n".join(lines)


# ---- publishing: redaction, code stripping, excerpts -------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-(?:or-v1-|ant-[a-z0-9]+-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]
_HOST_PATTERNS = [
    re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:local|lan|internal|home|corp|ts\.net|tailnet)\b"),
    re.compile(r"\b(?:10|127)(?:\.\d{1,3}){3}\b|\b192\.168(?:\.\d{1,3}){2}\b"
               r"|\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b"),
    re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"),
]
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)
_HOST_ENV = ("WATERLOO_SSH_HOST", "OLLAMA_URL", "ANTHROPIC_BASE_URL", "OPENROUTER_BASE_URL")


def sensitive_literals(env: dict[str, str] | None = None, extra: Iterable[str] = ()) -> list[str]:
    """Exact strings to scrub: secret-looking env values, judge/gateway hosts, this hostname,
    and ``CCO_REDACT`` (comma-separated). Values are only compared, never printed."""
    env = dict(os.environ if env is None else env)
    values = [v for k, v in env.items() if _SECRET_ENV.search(k) and len(v) >= 8]
    for name in _HOST_ENV:
        raw = env.get(name, "")
        host = re.sub(r"^[a-z]+://", "", raw).split("/")[0].split(":")[0]
        values += [raw, host, host.split("@")[-1]]
    values += [s.strip() for s in env.get("CCO_REDACT", "").split(",")]
    try:
        values.append(socket.gethostname())
    except OSError:
        pass
    values += list(extra)
    public = {"openrouter.ai", "api.anthropic.com", "localhost", "http", "https"}
    return sorted({v for v in values if v and len(v) >= 4 and v not in public}, key=len, reverse=True)


def redact(text: str, literals: Iterable[str] = ()) -> tuple[str, int]:
    count = 0
    for literal in literals:
        if literal in text:
            count += text.count(literal)
            text = text.replace(literal, "[REDACTED]")
    for pattern in (*_SECRET_PATTERNS, *_HOST_PATTERNS):
        text, n = pattern.subn("[REDACTED]", text)
        count += n
    return text, count


_FENCE = re.compile(r"```[^\n]*\n.*?(?:```|\Z)", re.S)


def strip_code(text: str) -> tuple[str, int]:
    """Replace fenced code blocks so a published trace never carries a full program."""
    blocks = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal blocks
        blocks += 1
        return f"[code block omitted from the published trace: {match.group(0).count(chr(10)) - 1} lines]"

    return _FENCE.sub(replace, text), blocks


def excerpt(text: str, chars: int = EXCERPT_CHARS) -> str:
    if len(text) <= 2 * chars + 40:
        return text
    head, tail = text[:chars], text[-chars:]
    return f"{head.rstrip()}\n\n[… {len(text) - 2 * chars:,} characters omitted …]\n\n{tail.lstrip()}"


def public_trace(trace: dict[str, Any], literals: Iterable[str]) -> tuple[dict[str, Any], str]:
    """A publishable copy of ``reasoning.json`` and its readable text, with secrets and host names
    redacted, fenced code removed and encrypted blobs reduced to their size."""
    literals = list(literals)
    redactions = code_blocks = 0

    def clean(value: str | None) -> str | None:
        nonlocal redactions, code_blocks
        if not value:
            return value
        value, n = redact(value, literals)
        value, c = strip_code(value)
        redactions, code_blocks = redactions + n, code_blocks + c
        return value

    turns = []
    for record in trace.get("turns", []):
        details = []
        for item in record.get("reasoning_details") or []:
            item = dict(item)
            if item.get("type") == "reasoning.encrypted" and item.get("data"):
                item["data"] = f"[encrypted: {len(str(item['data'])):,} characters, not published]"
            for key in ("text", "summary"):
                if isinstance(item.get(key), str):
                    item[key] = clean(item[key])
            if item.get("signature"):
                item["signature"] = "[signature omitted]"
            details.append(item)
        turns.append({**record, "reasoning": clean(record.get("reasoning")), "reasoning_details": details})
    published = {**trace, "turns": turns,
                 "published": {"redactions": redactions, "code_blocks_omitted": code_blocks}}
    text = render_markdown(published)
    # Belt and braces: the rendered text is scanned again in case a detail split a secret.
    text, extra = redact(text, literals)
    published["published"]["redactions"] += extra
    return published, text


def published_excerpt(trace: dict[str, Any], chars: int = EXCERPT_CHARS) -> str:
    texts = [ReasoningTurn(r.get("reasoning"), r.get("reasoning_details") or []).visible_text()
             for r in trace.get("turns", [])]
    return excerpt("\n\n".join(t for t in texts if t), chars)

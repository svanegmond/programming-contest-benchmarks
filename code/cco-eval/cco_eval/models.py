"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GenerateResult:
    text: str
    provider: str
    model: str
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    raw_provider_metadata: dict[str, object] | None = None
    error: str | None = None
    # OpenRouter ``message.reasoning`` and ``message.reasoning_details``; kept off ``to_record``
    # so attempt.json stays small. Runners save them to reasoning.json.
    reasoning: str | None = None
    reasoning_details: list[dict[str, object]] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_record(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "stop_reason": self.stop_reason,
            "raw_provider_metadata": self.raw_provider_metadata or {},
            "error": self.error,
        }

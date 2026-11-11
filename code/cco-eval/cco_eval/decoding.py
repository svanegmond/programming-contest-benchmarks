"""OpenRouter catalog lookup, token caps, and reasoning-effort policy."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# Unknown or under-specified reasoning models must never silently fall back to 32k.
REASONING_TOKEN_FLOOR = 65536
PROMPT_VERSION = "cco-v2"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_HTTP_TIMEOUT_S = 7200.0
EFFORT_RANK = ("high", "medium", "low", "minimal")


def model_id(model: str) -> str:
    return model.removeprefix("openrouter/")


def load_openrouter_catalog(
    *,
    url: str = OPENROUTER_MODELS_URL,
    timeout: float = 30.0,
    opener: Any = None,
) -> dict[str, dict[str, Any]]:
    """Public catalog; no API key. Returns id -> model record."""
    request = urllib.request.Request(url, headers={"User-Agent": "cco-eval/1.0"}, method="GET")
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    catalog: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            catalog[str(row["id"])] = row
    return catalog


def lookup_model(model: str, catalog: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    if not catalog:
        return None
    return catalog.get(model_id(model))


def _supported_parameters(entry: dict[str, Any] | None) -> set[str]:
    if not entry:
        return set()
    raw = entry.get("supported_parameters") or []
    return {str(item) for item in raw}


def _supported_efforts(entry: dict[str, Any] | None) -> list[str]:
    if not entry:
        return []
    reasoning = entry.get("reasoning")
    if isinstance(reasoning, dict):
        raw = reasoning.get("supported_efforts") or []
        return [str(item) for item in raw]
    return []


def supports_reasoning(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return True
    params = _supported_parameters(entry)
    if {"reasoning", "reasoning_effort", "include_reasoning"} & params:
        return True
    reasoning = entry.get("reasoning")
    return isinstance(reasoning, dict) and bool(reasoning)


def catalog_max_completion_tokens(entry: dict[str, Any] | None) -> int | None:
    if not entry:
        return None
    provider = entry.get("top_provider")
    if isinstance(provider, dict):
        value = provider.get("max_completion_tokens")
        if isinstance(value, int) and value > 0:
            return value
    return None


def resolve_max_tokens(
    model: str,
    override: int | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> int:
    if override is not None:
        if override < 1:
            raise ValueError("max_tokens must be positive")
        return override
    entry = lookup_model(model, catalog)
    catalog_max = catalog_max_completion_tokens(entry)
    if supports_reasoning(entry):
        if catalog_max is None:
            return REASONING_TOKEN_FLOOR
        return max(catalog_max, REASONING_TOKEN_FLOOR)
    if catalog_max is None:
        return REASONING_TOKEN_FLOOR
    return catalog_max


def resolve_reasoning_effort(
    model: str,
    requested: str | None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """Return the effort string to send, or None to omit the parameter."""
    if requested in {None, "none"}:
        return None
    entry = lookup_model(model, catalog)
    if entry is None:
        return requested
    efforts = _supported_efforts(entry)
    if not supports_reasoning(entry) and not efforts:
        return None
    if efforts and requested not in efforts:
        for candidate in EFFORT_RANK:
            if candidate in efforts:
                return candidate
        return efforts[0]
    return requested


def supported_parameters(model: str, catalog: dict[str, dict[str, Any]] | None) -> list[str] | None:
    entry = lookup_model(model, catalog)
    if not entry:
        return None
    return sorted(_supported_parameters(entry))

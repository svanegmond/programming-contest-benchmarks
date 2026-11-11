from __future__ import annotations

import json
from pathlib import Path

from cco_eval.decoding import (
    REASONING_TOKEN_FLOOR,
    load_openrouter_catalog,
    resolve_max_tokens,
    resolve_reasoning_effort,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/openrouter/models.json").read_text())
CATALOG = {row["id"]: row for row in FIXTURE["data"]}


def test_catalog_parse_and_reasoning_floor():
    catalog = load_openrouter_catalog(opener=lambda request, timeout: _Response(FIXTURE))
    assert resolve_max_tokens("openrouter/google/gemini-3.8-flash", catalog=catalog) == 65536
    assert resolve_max_tokens("openrouter/openai/gpt-oss-20b", catalog=catalog) == REASONING_TOKEN_FLOOR
    assert resolve_max_tokens("openrouter/unknown-reasoner", catalog=catalog) == REASONING_TOKEN_FLOOR
    assert resolve_max_tokens("qwen/qwen3-coder", catalog=catalog) == 16384
    assert resolve_max_tokens("openai/gpt-oss-20b", 12000, catalog=catalog) == 12000


def test_effort_is_high_by_default_and_omitted_for_non_reasoners():
    assert resolve_reasoning_effort("openai/gpt-oss-20b", "high", CATALOG) == "high"
    assert resolve_reasoning_effort("openai/gpt-oss-20b", "low", CATALOG) == "low"
    assert resolve_reasoning_effort("openai/gpt-oss-20b", "none", CATALOG) is None
    assert resolve_reasoning_effort("qwen/qwen3-coder", "high", CATALOG) is None
    assert resolve_reasoning_effort("openrouter/unknown", "high", CATALOG) == "high"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()

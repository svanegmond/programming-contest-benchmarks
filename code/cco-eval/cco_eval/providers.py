"""LLM provider adapters: Ollama, Anthropic, generate_text dispatcher."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from cco_eval.models import GenerateResult
from cco_eval.tracing import Span
from cco_eval.utils import int_or_none, ms_since, sha256_text

DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DIRECT_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Set ANTHROPIC_BASE_URL to route through a gateway; the direct API is the fallback.
DEFAULT_ANTHROPIC_URL = os.environ.get("ANTHROPIC_BASE_URL", DIRECT_ANTHROPIC_URL)
DEFAULT_OPENROUTER_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


def ollama_metadata(parsed: dict[str, object]) -> dict[str, object]:
    keys = [
        "created_at",
        "done",
        "done_reason",
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    ]
    return {key: parsed[key] for key in keys if key in parsed}


def generate_ollama(
    model: str, prompt: str, ollama_url: str, timeout: float, max_tokens: int = 0
) -> GenerateResult:
    start = time.perf_counter()
    options = {"temperature": 0}
    # num_predict controls output cap; -1 = unlimited. max_tokens<=0 means uncapped.
    options["num_predict"] = -1 if max_tokens <= 0 else max_tokens
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ollama_url.rstrip("/") + "/api/generate",
        data=data,
        headers={
            "Content-Type": "application/json",
            # Default Python-urllib UA trips Cloudflare's edge WAF (error 1010)
            # now that Ollama is fronted by the Cloudflare Tunnel; a normal UA passes.
            "User-Agent": "cco-eval/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    return GenerateResult(
        text=str(parsed.get("response", "")),
        provider="ollama",
        model=model,
        latency_ms=ms_since(start),
        input_tokens=int_or_none(parsed.get("prompt_eval_count")),
        output_tokens=int_or_none(parsed.get("eval_count")),
        stop_reason=str(parsed.get("done_reason")) if parsed.get("done_reason") is not None else None,
        raw_provider_metadata=ollama_metadata(parsed),
    )


def anthropic_metadata(parsed: dict[str, object]) -> dict[str, object]:
    keys = ["id", "type", "role", "model", "stop_reason", "stop_sequence", "usage"]
    return {key: parsed[key] for key in keys if key in parsed}


def generate_anthropic(
    model: str,
    prompt: str,
    api_key: str,
    timeout: float,
    max_tokens: int = 128,
) -> GenerateResult:
    start = time.perf_counter()

    def _post(url: str, include_temperature: bool) -> str:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if include_temperature:
            payload["temperature"] = 0
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                # Default Python-urllib UA trips Cloudflare's edge WAF (error 1010)
                # when routed through AI Gateway; a normal UA passes cleanly.
                "User-Agent": "cco-eval/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    # Newer models deprecate `temperature`; retry without it on that specific 400.
    # Also retry transient overload/rate-limit (429/529) with exponential backoff.
    # If the AI Gateway itself is unreachable (network/DNS failure, not an HTTP
    # error response), fall back to calling Anthropic directly so a Gateway outage
    # never blocks a benchmark run.
    include_temperature = True
    body = None
    last_exc: Exception | None = None
    url = DEFAULT_ANTHROPIC_URL
    fell_back = False
    for attempt in range(5):
        try:
            body = _post(url, include_temperature=include_temperature)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_exc = exc
            if exc.code == 400 and "temperature" in detail:
                include_temperature = False
                continue
            if exc.code in (429, 529):
                time.sleep(min(2 ** attempt, 16))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if url != DIRECT_ANTHROPIC_URL and not fell_back:
                url = DIRECT_ANTHROPIC_URL
                fell_back = True
                continue
            raise
    if body is None:
        raise last_exc if last_exc else RuntimeError("anthropic request failed")
    parsed = json.loads(body)
    parts: list[str] = []
    for block in parsed.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    usage = parsed.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    return GenerateResult(
        text="\n".join(parts),
        provider="anthropic",
        model=model,
        latency_ms=ms_since(start),
        input_tokens=int_or_none(usage.get("input_tokens")),
        output_tokens=int_or_none(usage.get("output_tokens")),
        stop_reason=str(parsed.get("stop_reason")) if parsed.get("stop_reason") is not None else None,
        raw_provider_metadata=anthropic_metadata(parsed),
    )


def generate_openrouter(
    model: str,
    messages: list[dict[str, object]],
    api_key: str,
    timeout: float,
    max_tokens: int = 4096,
    *,
    tools: list[dict[str, object]] | None = None,
    base_url: str = DEFAULT_OPENROUTER_URL,
    reasoning_effort: str | None = None,
    temperature: float = 0.0,
    supported_parameters: list[str] | None = None,
) -> GenerateResult:
    """Call OpenRouter's OpenAI-compatible chat completions endpoint."""
    start = time.perf_counter()
    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    params = set(supported_parameters) if supported_parameters is not None else {"temperature"}
    if supported_parameters is None or "temperature" in params:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/svanegmond/programming-contest-benchmarks",
            "X-Title": "programming-contest-benchmarks CCO eval",
            "User-Agent": "cco-eval/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    choices = parsed.get("choices", [])
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    usage = parsed.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    metadata: dict[str, object] = {
        "id": parsed.get("id"),
        "model": parsed.get("model", model),
        "usage": usage,
        "provider": parsed.get("provider"),
        "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
        "tool_calls": message.get("tool_calls", []),
    }
    metadata = {key: value for key, value in metadata.items() if value is not None}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = str(content or "")
    return GenerateResult(
        text=text,
        provider="openrouter",
        model=str(parsed.get("model", model)),
        latency_ms=ms_since(start),
        input_tokens=int_or_none(usage.get("prompt_tokens")),
        output_tokens=int_or_none(usage.get("completion_tokens")),
        stop_reason=str(choice["finish_reason"]) if isinstance(choice, dict) and choice.get("finish_reason") is not None else None,
        raw_provider_metadata=metadata,
    )


def generate_text(
    provider: str,
    model: str,
    prompt: str,
    *,
    dry_run: bool,
    dry_response: str,
    mock_response: str | None,
    ollama_url: str,
    ollama_timeout: float,
    anthropic_api_key: str | None,
    anthropic_timeout: float,
    max_tokens: int,
    openrouter_api_key: str | None = None,
    openrouter_timeout: float = 180.0,
    openrouter_base_url: str = DEFAULT_OPENROUTER_URL,
    messages: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
) -> GenerateResult:
    if dry_run:
        return GenerateResult(
            text=dry_response,
            provider=provider,
            model=model,
            raw_provider_metadata={"dry_run": True},
        )
    if mock_response is not None:
        return GenerateResult(
            text=load_mock_response(mock_response),
            provider=provider,
            model=model,
            raw_provider_metadata={"mock_response": True},
        )
    if provider == "openrouter":
        api_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("missing OpenRouter API key")
        return generate_openrouter(
            model,
            messages or [{"role": "user", "content": prompt}],
            api_key,
            openrouter_timeout,
            max_tokens,
            tools=tools,
            base_url=openrouter_base_url,
        )
    if provider == "anthropic":
        api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("missing Anthropic API key")
        return generate_anthropic(model, prompt, api_key, anthropic_timeout, max_tokens)
    if provider == "ollama":
        return generate_ollama(model, prompt, ollama_url, ollama_timeout, max_tokens)
    raise RuntimeError(f"unsupported provider: {provider}")


def mark_generation_span(span: Span, generation: GenerateResult) -> None:
    span.set_attribute("response_sha256", sha256_text(generation.text))
    span.set_attribute("response_chars", len(generation.text))
    span.set_attribute("latency_ms", generation.latency_ms)
    span.set_attribute("input_tokens", generation.input_tokens)
    span.set_attribute("output_tokens", generation.output_tokens)
    span.set_attribute("stop_reason", generation.stop_reason)
    if generation.error:
        span.set_error(generation.error)


def generation_stage(generation: GenerateResult) -> dict[str, object]:
    stage: dict[str, object] = {
        "ok": generation.ok,
        "latency_ms": generation.latency_ms,
        "input_tokens": generation.input_tokens,
        "output_tokens": generation.output_tokens,
        "stop_reason": generation.stop_reason,
    }
    if generation.error:
        stage["reason"] = generation.error
    return stage


def dry_run_response() -> str:
    return """```python
import sys


def main():
    sys.stdin.read()


if __name__ == "__main__":
    main()
```"""


def load_mock_response(value: str) -> str:
    candidate = Path(value)
    if candidate.exists():
        return candidate.read_text(encoding="utf-8", errors="replace")
    return value

"""OpenRouter Batch API: submit one batch per model, poll without a wall clock, resume by job id.

OpenRouter publishes batch pricing on ``<model>:batch`` catalog ids, but those ids are not
callable on ``/chat/completions``. A batch is submitted to ``POST /api/v1/batches`` with the
*base* slug and polled at ``GET /api/v1/batches/{id}``. See
https://openrouter.ai/docs/batch-quickstart.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cco_eval.decoding import lookup_model, model_id
from cco_eval.models import GenerateResult
from cco_eval.providers import DEFAULT_OPENROUTER_URL, openrouter_headers, parse_openrouter_chat
from cco_eval.utils import write_json

BATCH_SUFFIX = ":batch"
FINAL_STATUSES = frozenset({"completed", "failed", "expired", "cancelled"})
CHAT_ENDPOINT = "/v1/chat/completions"
POLL_START_S = 30.0
POLL_MAX_S = 300.0
RATE_LIMIT_WAIT_S = 120.0


def is_batch_model(model: str) -> bool:
    return model.endswith(BATCH_SUFFIX)


def as_batch_model(model: str) -> str:
    return model if is_batch_model(model) else model + BATCH_SUFFIX


def submit_slug(model: str) -> str:
    """The slug the Batch API expects: base model, no ``openrouter/`` prefix, no ``:batch``."""
    return model_id(model).removesuffix(BATCH_SUFFIX)


class BatchHTTPError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"OpenRouter batch API returned {status}: {message}")
        self.status = status


class OpenRouterHTTP:
    """Minimal JSON client. Tests replace it with a fake that has the same ``request`` method."""

    def __init__(self, api_key: str | None = None, base_url: str = DEFAULT_OPENROUTER_URL,
                 timeout: float = 600.0):
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("missing OPENROUTER_API_KEY")
        self._key = key
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self._base + path, data=data,
                                         headers=openrouter_headers(self._key), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, _json(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return json.dumps(payload)[:300]


def remaining_credits(http: Any) -> float:
    status, payload = http.request("GET", "/credits")
    if status != 200:
        raise BatchHTTPError(status, _error_message(payload))
    data = payload.get("data") or {}
    return float(data["total_credits"]) - float(data["total_usage"])


def estimate_prompt_tokens(messages: list[dict[str, object]]) -> int:
    """Deliberately high: ~3 characters per token plus per-message overhead."""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return math.ceil(chars / 3) + 16 * len(messages)


def worst_case_cost(model: str, catalog: dict[str, Any] | None, prompt_tokens: int, max_tokens: int) -> float:
    """Prompt plus a full ``max_tokens`` completion, at the ``:batch`` catalog price."""
    entry = lookup_model(model, catalog)
    pricing = (entry or {}).get("pricing") or {}
    try:
        prompt_price = float(pricing["prompt"])
        completion_price = float(pricing["completion"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"no catalog pricing for {model_id(model)}; cannot bound batch cost") from exc
    return prompt_tokens * prompt_price + max_tokens * completion_price


def ensure_affordable(http: Any, worst_case_usd: float) -> float:
    remaining = remaining_credits(http)
    if remaining < worst_case_usd:
        raise RuntimeError(
            f"OpenRouter credits ${remaining:.4f} are below the batch worst case ${worst_case_usd:.4f} "
            "(prompt + max_tokens at batch prices); lower --max-tokens or add credits")
    return remaining


@dataclass(frozen=True)
class BatchRequest:
    custom_id: str
    body: dict[str, Any]


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def submit_or_resume(model: str, requests: list[BatchRequest], state_path: Path, http: Any,
                     *, now: Callable[[], float] = time.time,
                     log: Callable[[str], None] = print) -> dict[str, Any]:
    """Return the batch state, submitting only when ``state_path`` has no job id yet."""
    state = _read(state_path)
    if state and state.get("batch_id"):
        log(f"batch resume model={model} id={state['batch_id']} status={state.get('status')}")
        return state
    if not requests:
        raise ValueError("a batch needs at least one request")
    # Field order matters: OpenRouter streams the body and needs metadata before `requests`.
    body = {"endpoint": CHAT_ENDPOINT, "model": submit_slug(model), "completion_window": "24h",
            "requests": [{"custom_id": r.custom_id, "body": r.body} for r in requests]}
    status, payload = http.request("POST", "/batches", body)
    if status not in (200, 201, 202) or not payload.get("id"):
        # Not retried: a lost 202 would leave a paid batch running alongside a second one.
        raise BatchHTTPError(status, _error_message(payload))
    submitted = float(payload.get("created_at") or now())
    state = {"batch_id": payload["id"], "model": model, "submit_model": submit_slug(model),
             "endpoint": CHAT_ENDPOINT, "custom_ids": [r.custom_id for r in requests],
             "submitted_at": _iso(submitted), "submitted_unix": submitted,
             "status": payload.get("status"), "provider": payload.get("provider"),
             "expires_at": payload.get("expires_at")}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(state_path, state)
    log(f"batch submitted model={model} id={payload['id']} requests={len(requests)}")
    return state


def poll(state: dict[str, Any], state_path: Path, http: Any, *,
         sleep: Callable[[float], None] | None = None, now: Callable[[], float] = time.time,
         log: Callable[[str], None] = print, start_s: float = POLL_START_S,
         max_s: float = POLL_MAX_S) -> dict[str, Any]:
    """Poll until a final status with results. No wall-clock limit, by design."""
    sleep = sleep or time.sleep
    delay = start_s
    last_status = state.get("status")
    while True:
        try:
            status, payload = http.request("GET", f"/batches/{state['batch_id']}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log(f"batch poll transport error ({exc}); retrying in {delay:.0f}s")
            sleep(delay)
            delay = min(delay * 2, max_s)
            continue
        if status == 200:
            batch_status = payload.get("status")
            results_ready = batch_status != "completed" or payload.get("results") is not None
            if batch_status in FINAL_STATUSES and results_ready:
                finished = float(payload.get("finalized_at") or now())
                state.update({
                    "status": batch_status, "provider": payload.get("provider") or state.get("provider"),
                    "completed_at": _iso(finished), "completed_unix": finished,
                    "latency_s": round(finished - float(state["submitted_unix"]), 3),
                    "request_counts": payload.get("request_counts"), "usage": payload.get("usage"),
                    "error": payload.get("error"),
                })
                write_json(state_path, state)
                log(f"batch final model={state['model']} id={state['batch_id']} status={batch_status} "
                    f"latency_s={state['latency_s']}")
                return payload
            if batch_status != last_status:
                last_status = batch_status
                state["status"] = batch_status
                write_json(state_path, state)
                log(f"batch status id={state['batch_id']} status={batch_status} "
                    f"counts={payload.get('request_counts')}")
            wait = delay
            delay = min(delay * 1.5, max_s)
        elif status == 429:
            wait = RATE_LIMIT_WAIT_S
        elif status >= 500:
            wait = delay
            delay = min(delay * 2, max_s)
        else:
            # 402, 404 and 410 do not clear by retrying.
            raise BatchHTTPError(status, _error_message(payload))
        sleep(wait)


def generation_cost(http: Any, generation_id: str) -> float | None:
    try:
        status, payload = http.request("GET", f"/generation?id={generation_id}")
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None
    data = payload.get("data") if status == 200 else None
    cost = data.get("total_cost") if isinstance(data, dict) else None
    return float(cost) if isinstance(cost, (int, float)) else None


def map_results(batch: dict[str, Any], state: dict[str, Any], http: Any | None = None) -> dict[str, GenerateResult | str]:
    """custom_id -> GenerateResult, or an error string for requests the batch did not answer."""
    results = batch.get("results") or []
    latency_ms = round(float(state.get("latency_s") or 0) * 1000, 3)
    batch_meta = {key: state.get(key) for key in (
        "batch_id", "submit_model", "submitted_at", "completed_at", "latency_s", "status", "provider")}
    batch_usage = batch.get("usage") if isinstance(batch.get("usage"), dict) else {}
    out: dict[str, GenerateResult | str] = {}
    for item in results:
        if not isinstance(item, dict) or not item.get("custom_id"):
            continue
        cid = str(item["custom_id"])
        response = item.get("response") if isinstance(item.get("response"), dict) else {}
        body = response.get("body") if isinstance(response.get("body"), dict) else None
        status_code = response.get("status_code")
        if item.get("error") or body is None or (isinstance(status_code, int) and status_code >= 400):
            err = item.get("error") or (body or {}).get("error") or {"message": f"status {status_code}"}
            out[cid] = str(err.get("message") if isinstance(err, dict) else err)
            continue
        generation = parse_openrouter_chat(body, state["submit_model"], latency_ms)
        meta = dict(generation.raw_provider_metadata or {})
        usage = dict(meta.get("usage") or {})
        if usage.get("cost") is None:
            if len(results) == 1 and batch_usage.get("cost") is not None:
                usage["cost"] = batch_usage.get("cost")
                usage["cost_source"] = "batch.usage"
                if "is_byok" in batch_usage:
                    usage["is_byok"] = batch_usage["is_byok"]
            elif http is not None and body.get("id"):
                cost = generation_cost(http, str(body["id"]))
                if cost is not None:
                    usage["cost"], usage["cost_source"] = cost, "generation"
        meta["usage"] = usage
        meta.setdefault("provider", state.get("provider"))
        meta["batch"] = batch_meta
        generation.raw_provider_metadata = meta
        out[cid] = generation
    for cid in state.get("custom_ids", []):
        if cid not in out:
            message = (batch.get("error") or {}).get("message") if isinstance(batch.get("error"), dict) else None
            out[cid] = message or f"batch {state.get('status')} without a result for this request"
    return out


def archive(state_path: Path, state: dict[str, Any]) -> Path:
    """Retire the active job file once its results are recorded, so the next run submits fresh."""
    target = state_path.with_name(f"{state_path.name.removesuffix('.active.json')}.{state['batch_id']}.json")
    write_json(target, state)
    state_path.unlink()
    return target

"""Minimal OTEL-shaped JSONL tracer."""

from __future__ import annotations

import contextlib
import json
import time
import traceback
import uuid
from pathlib import Path

from cco_eval.utils import utc_now


class Span:
    def __init__(self, name: str, attributes: dict[str, object] | None = None) -> None:
        self.name = name
        self.attributes = dict(attributes or {})
        self.status_code = "OK"
        self.status_message = ""
        self.exception: dict[str, object] | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_error(self, message: str, exc: BaseException | None = None) -> None:
        self.status_code = "ERROR"
        self.status_message = message
        if exc is not None:
            self.exception = {
                "type": type(exc).__name__,
                "message": str(exc),
                "stacktrace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }


class JsonlTracer:
    """Minimal OTEL-shaped JSONL tracer for dependency-light local runs."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.trace_id = uuid.uuid4().hex
        self.stack: list[str] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    @contextlib.contextmanager
    def span(self, name: str, attributes: dict[str, object] | None = None):
        span = Span(name, attributes)
        span_id = uuid.uuid4().hex[:16]
        parent_span_id = self.stack[-1] if self.stack else None
        start_time = utc_now()
        start_perf = time.perf_counter()
        self.stack.append(span_id)
        try:
            yield span
        except BaseException as exc:
            span.set_error(str(exc), exc)
            raise
        finally:
            self.stack.pop()
            end_time = utc_now()
            record = {
                "trace_id": self.trace_id,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "name": name,
                "start_time": start_time,
                "end_time": end_time,
                "duration_ms": round((time.perf_counter() - start_perf) * 1000, 3),
                "attributes": span.attributes,
                "status": {"code": span.status_code, "message": span.status_message},
                "exception": span.exception,
            }
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

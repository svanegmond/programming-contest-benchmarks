from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from core import IsolateRunner


def _read_line(fd: int, buffer: bytearray) -> bytes:
    while b"\n" not in buffer:
        block = os.read(fd, 1 << 20)
        if not block:
            time.sleep(0.02)
            continue
        buffer.extend(block)
    index = buffer.index(b"\n")
    line = bytes(buffer[: index + 1])
    del buffer[: index + 1]
    return line


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def serve() -> None:
    runner = IsolateRunner()
    sessions: dict[str, tuple[str, Path]] = {}
    root = Path(tempfile.gettempdir()) / "waterloo-judge-sessions"
    root.mkdir(parents=True, exist_ok=True)
    port_path = "/dev/virtio-ports/org.waterloo.judge"
    while not os.path.exists(port_path):
        time.sleep(0.25)
    fd = os.open(port_path, os.O_RDWR)
    buffer = bytearray()
    while True:
        line = _read_line(fd, buffer)
        response: dict[str, Any]
        try:
            request = json.loads(line)
            operation = request.get("op")
            if operation == "ping":
                response = {"ok": True, "pong": True}
            elif operation == "env-id":
                response = {
                    "ok": True,
                    "env_id": Path("/etc/waterloo-env-id").read_text().strip(),
                }
            elif operation == "prepare":
                lang = str(request["lang"])
                source = base64.b64decode(request["source"], validate=True)
                session_id = uuid.uuid4().hex
                workdir = root / session_id
                compile_result = runner.prepare(lang, source, workdir)
                if compile_result["ok"]:
                    sessions[session_id] = (lang, Path(compile_result["artifact"]))
                    compile_result["artifact"] = session_id
                else:
                    shutil.rmtree(workdir, ignore_errors=True)
                response = {"ok": True, "compile": compile_result}
            elif operation == "run":
                session_id = str(request["session_id"])
                lang, artifact = sessions[session_id]
                result = runner.run_artifact(
                    lang,
                    artifact,
                    base64.b64decode(request["stdin"], validate=True),
                    float(request["time_limit_s"]),
                    float(request.get("kill_factor", 3)),
                )
                result["stdout"] = base64.b64encode(result["stdout"]).decode("ascii")
                result["stderr"] = base64.b64encode(result["stderr"]).decode("ascii")
                response = {"ok": True, "result": result}
            elif operation == "release":
                session = sessions.pop(str(request["session_id"]), None)
                if session:
                    shutil.rmtree(session[1].parent, ignore_errors=True)
                response = {"ok": True}
            else:
                response = {"ok": False, "error": f"unknown operation: {operation}"}
        except Exception as error:
            response = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(limit=4),
            }
        _write_all(fd, json.dumps(response, separators=(",", ":")).encode() + b"\n")


if __name__ == "__main__":
    serve()

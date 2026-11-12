from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

try:
    from cco_eval.waterloo import (
        GIVE_UP_TIMEOUT_S,
        Outcome3,
        TestResult,
        TestVerdict,
        Verdict,
        sandbox_timeout_s,
    )
except ModuleNotFoundError:
    from dataclasses import dataclass
    from enum import StrEnum

    GIVE_UP_TIMEOUT_S = 120.0

    def sandbox_timeout_s(time_limit_s: float, give_up_s: float | None = None) -> float:
        give_up = GIVE_UP_TIMEOUT_S if give_up_s is None else float(give_up_s)
        return max(float(time_limit_s), give_up)

    class Outcome3(StrEnum):
        PASS_ON_TIME = "pass_on_time"
        PASS_OVER_TIME = "pass_over_time"
        FAIL = "fail"

    class TestVerdict(StrEnum):
        PASS = "pass"
        WRONG_ANSWER = "wrong_answer"
        RUNTIME_ERROR = "runtime_error"
        KILLED = "killed"
        COMPILE_ERROR = "compile_error"

    @dataclass
    class TestResult:
        attempt_id: str
        test_id: str
        subtask: str
        verdict: TestVerdict
        cpu_s: float | None
        wall_s: float | None
        peak_memory_mb: float | None
        over_time: bool
        over_memory: bool
        score: float | None = None

    @dataclass
    class Verdict:
        attempt_id: str
        problem_id: str
        subtask_pass: dict[str, bool]
        all_pass: bool
        partial_score: float
        outcome: Outcome3
        env_id: str


HARD_MEMORY_MB = 2048
CHECKER_KINDS = ("validator", "scorer")
OUTPUT_LIMIT = 16_384
STDERR_TAIL_LIMIT = 4_000
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def output_tokens(data: bytes) -> list[bytes]:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split()


def outputs_match(
    actual: bytes,
    expected: bytes,
    grading_kind: str,
    absolute_tolerance: float = 1e-6,
    relative_tolerance: float = 1e-6,
) -> bool:
    if (
        not math.isfinite(absolute_tolerance)
        or not math.isfinite(relative_tolerance)
        or absolute_tolerance < 0
        or relative_tolerance < 0
    ):
        raise ValueError("float tolerances must be nonnegative")
    actual_tokens = output_tokens(actual)
    expected_tokens = output_tokens(expected)
    if len(actual_tokens) != len(expected_tokens):
        return False
    if grading_kind == "exact":
        return actual_tokens == expected_tokens
    if grading_kind != "float_tol":
        raise ValueError(f"unsupported deterministic grading kind: {grading_kind}")
    for actual_token, expected_token in zip(actual_tokens, expected_tokens):
        try:
            actual_text = actual_token.decode("utf-8")
            expected_text = expected_token.decode("utf-8")
        except UnicodeDecodeError:
            if actual_token != expected_token:
                return False
            continue
        actual_numeric = NUMBER.fullmatch(actual_text) is not None
        expected_numeric = NUMBER.fullmatch(expected_text) is not None
        if not actual_numeric or not expected_numeric:
            if actual_token != expected_token:
                return False
            continue
        actual_number = float(actual_text)
        expected_number = float(expected_text)
        if not math.isclose(
            actual_number,
            expected_number,
            rel_tol=relative_tolerance,
            abs_tol=absolute_tolerance,
        ):
            return False
    return True


def classify_outcome(all_pass: bool, any_over_time: bool) -> Outcome3:
    if not all_pass:
        return Outcome3.FAIL
    if any_over_time:
        return Outcome3.PASS_OVER_TIME
    return Outcome3.PASS_ON_TIME


def _decode_meta(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip()] = value.strip()
    return values


def _number(meta: dict[str, str], key: str) -> float | None:
    try:
        return float(meta[key])
    except (KeyError, ValueError):
        return None


class IsolateRunner:
    def __init__(self, rootfs: Path | None = None, isolate: str = "isolate"):
        self.rootfs = rootfs or Path(
            os.environ.get("WATERLOO_ROOTFS", "/srv/waterloo/image/mounted-root")
        )
        self.isolate = isolate

    def _base_args(self) -> list[str]:
        args = [self.isolate, "--box-id=0", "--cg", "--env=PATH=/usr/local/bin:/usr/bin"]
        if self.rootfs.exists():
            if not self.rootfs.is_mount():
                raise RuntimeError(f"Arch rootfs must be mounted read-only: {self.rootfs}")
            if not os.statvfs(self.rootfs).f_flag & os.ST_RDONLY:
                raise RuntimeError(f"Arch rootfs mount is writable: {self.rootfs}")
            base = self.rootfs
        else:
            base = Path("/")
        if not (base / "etc/waterloo-env-id").is_file():
            raise RuntimeError(f"Arch rootfs has no environment marker: {base}")
        for directory in ("bin", "etc", "lib", "lib64", "usr", "opt/pypy3"):
            source = base / directory
            if source.exists():
                args.append(f"--dir=/{directory}={source}")
        dev_source = base / "usr/share/waterloo/judge-dev"
        if not dev_source.is_dir():
            raise RuntimeError(f"sanitized judge /dev directory is missing: {dev_source}")
        args.append(f"--dir=/dev={dev_source}:dev")
        args.append("--dir=/dev/shm=")
        return args

    def _box(self) -> tuple[Path, list[str]]:
        args = self._base_args()
        created = subprocess.run(
            [*args, "--init"], capture_output=True, text=True, check=False
        )
        if created.returncode != 0:
            raise RuntimeError(f"isolate --init failed: {created.stderr[-STDERR_TAIL_LIMIT:]}")
        box = Path(created.stdout.strip().splitlines()[-1]) / "box"
        return box, args

    def prepare(self, lang: str, source: bytes, workdir: Path) -> dict[str, Any]:
        workdir.mkdir(parents=True, exist_ok=True)
        if lang == "cpp":
            source_name = "submission.cpp"
            artifact_name = "submission"
            command = [
                "/usr/bin/g++",
                "-std=gnu++20",
                "-O2",
                "-pipe",
                "-static",
                "-DONLINE_JUDGE",
                f"/box/{source_name}",
                "-o",
                f"/box/{artifact_name}",
            ]
        elif lang == "python":
            source_name = "submission.py"
            artifact_name = source_name
            command = ["/usr/bin/pypy3", "-m", "py_compile", f"/box/{source_name}"]
        else:
            raise ValueError(f"unsupported language: {lang}")

        box, args = self._box()
        source_path = box / source_name
        source_path.write_bytes(source)
        out_path = box / "compile.stdout"
        err_path = box / "compile.stderr"
        out_path.touch(mode=0o666)
        err_path.touch(mode=0o666)
        meta_path = box / "compile.meta"
        compile_args = [
            *args,
            "--time=30",
            "--wall-time=90",
            f"--cg-mem={HARD_MEMORY_MB * 1024}",
            f"--stack={HARD_MEMORY_MB * 1024}",
            "--fsize=65536",
            "--processes=64",
            "--chdir=/box",
            "--stdout=/box/compile.stdout",
            "--stderr=/box/compile.stderr",
            f"--meta={meta_path}",
            "--run",
            "--",
            *command,
        ]
        try:
            process = subprocess.run(compile_args, capture_output=True, text=True, check=False)
            meta = _decode_meta(meta_path)
            stdout = out_path.read_bytes()
            stderr = err_path.read_bytes() or process.stderr.encode()
            ok = process.returncode == 0 and not ("killed" in meta or meta.get("status") == "TO")
            artifact_in_box = box / artifact_name
            if lang == "python":
                artifact_in_box = source_path
            artifact = workdir / artifact_name
            if ok and artifact_in_box.exists():
                shutil.copy2(artifact_in_box, artifact)
            else:
                ok = False
            return {
                "ok": ok,
                "returncode": process.returncode,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace")[-STDERR_TAIL_LIMIT:],
                "artifact": str(artifact) if ok else None,
            }
        finally:
            subprocess.run([*args, "--cleanup"], capture_output=True, check=False)

    def release(self, artifact: Path) -> None:
        shutil.rmtree(artifact.parent, ignore_errors=True)

    def run_artifact(
        self,
        lang: str,
        artifact: Path,
        stdin: bytes,
        time_limit_s: float,
        kill_factor: float,
    ) -> dict[str, Any]:
        effective_memory = HARD_MEMORY_MB
        if lang == "cpp":
            command = ["/box/submission"]
            artifact_name = "submission"
        else:
            command = ["/usr/bin/pypy3", "/box/submission.py"]
            artifact_name = "submission.py"
        box, args = self._box()
        shutil.copy2(artifact, box / artifact_name)
        input_path = box / "stdin"
        output_path = box / "stdout"
        error_path = box / "stderr"
        meta_path = box / "meta"
        input_path.write_bytes(stdin)
        for path in (output_path, error_path):
            path.touch(mode=0o666)
            path.chmod(0o666)
        run_args = [
            *args,
            f"--time={kill_factor * time_limit_s:g}",
            f"--wall-time={kill_factor * time_limit_s:g}",
            f"--cg-mem={effective_memory * 1024}",
            f"--stack={effective_memory * 1024}",
            "--fsize=65536",
            "--processes=64",
            "--chdir=/box",
            "--stdin=/box/stdin",
            "--stdout=/box/stdout",
            "--stderr=/box/stderr",
            f"--meta={meta_path}",
            "--run",
            "--",
            *command,
        ]
        started = time.monotonic()
        try:
            process = subprocess.run(run_args, capture_output=True, text=True, check=False)
            elapsed = time.monotonic() - started
            meta = _decode_meta(meta_path)
            stdout = output_path.read_bytes()
            stderr = error_path.read_bytes() or process.stderr.encode()
            cpu_s = _number(meta, "time")
            meta_wall_s = _number(meta, "time-wall")
            wall_s = meta_wall_s if meta_wall_s is not None else elapsed
            memory_kb = max(
                (value for value in (_number(meta, "max-rss"), _number(meta, "cg-mem")) if value is not None),
                default=None,
            )
            over_memory_cap = "cg-oom-killed" in meta
            killed = "killed" in meta or meta.get("status") == "TO" or over_memory_cap
            return {
                "returncode": process.returncode,
                "cpu_s": cpu_s,
                "wall_s": wall_s,
                "peak_memory_mb": memory_kb / 1024 if memory_kb is not None else None,
                "stdout": stdout,
                "stderr": stderr,
                "killed": killed,
                "over_memory_cap": over_memory_cap,
                "over_time": cpu_s is not None and cpu_s > time_limit_s,
                "meta": meta,
            }
        finally:
            subprocess.run([*args, "--cleanup"], capture_output=True, check=False)


class VmRunner:
    def __init__(self, socket_path: Path | None = None):
        self.socket_path = socket_path or Path(
            os.environ.get("WATERLOO_VM_SOCKET", "/srv/waterloo/judge-work/vm-control.sock")
        )

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        import socket

        # One recv can hold several replies (e.g. pongs for pings that queued while another
        # judge held QEMU's single control connection), so unread bytes carry over.
        buffer = bytearray()

        def read_line(connection: socket.socket) -> bytes:
            while b"\n" not in buffer:
                block = connection.recv(1 << 20)
                if not block:
                    raise RuntimeError("VM control channel closed before returning a response")
                buffer.extend(block)
                if len(buffer) > 128 * 1024 * 1024:
                    raise RuntimeError("VM control response exceeds 128 MiB")
            end = buffer.index(b"\n") + 1
            line = bytes(buffer[:end])
            del buffer[:end]
            return line

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(self.socket_path))
            # QEMU drops guest-bound bytes while the guest has not yet seen the new
            # host connection, so only send the real request after a ping is answered.
            connection.settimeout(1)
            for _ in range(30):
                connection.sendall(b'{"op":"ping"}\n')
                try:
                    if json.loads(read_line(connection)).get("pong"):
                        break
                except socket.timeout:
                    continue
            else:
                raise RuntimeError("VM guest agent did not answer ping")
            connection.settimeout(600)
            connection.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            while True:
                response = json.loads(read_line(connection))
                if not response.get("pong"):
                    break
        if not response.get("ok", False):
            raise RuntimeError(response.get("error", "VM judge request failed"))
        return response

    def prepare(self, lang: str, source: bytes, _workdir: Path) -> dict[str, Any]:
        response = self.request({
            "op": "prepare",
            "lang": lang,
            "source": base64.b64encode(source).decode("ascii"),
        })
        return response["compile"]

    def run_artifact(
        self,
        lang: str,
        artifact: Path,
        stdin: bytes,
        time_limit_s: float,
        kill_factor: float,
    ) -> dict[str, Any]:
        response = self.request({
            "op": "run",
            "session_id": artifact.name,
            "stdin": base64.b64encode(stdin).decode("ascii"),
            "time_limit_s": time_limit_s,
            "kill_factor": kill_factor,
        })
        result = response["result"]
        result["stdout"] = base64.b64decode(result["stdout"])
        result["stderr"] = base64.b64decode(result["stderr"])
        return result

    def release(self, artifact: Path) -> None:
        self.request({"op": "release", "session_id": artifact.name})


def _read_path(root: Path, specification: str) -> bytes:
    if "::" in specification:
        archive_name, member = specification.split("::", 1)
        archive = (root / archive_name).resolve()
        if not archive.is_relative_to(root.resolve()):
            raise ValueError(f"test archive path escapes --zips-root: {archive_name}")
        with zipfile.ZipFile(archive) as zipped:
            return zipped.read(member)
    path = (root / specification).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"test path escapes --zips-root: {specification}")
    return path.read_bytes()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _make_runner(backend: str) -> IsolateRunner | VmRunner:
    if backend == "host":
        return IsolateRunner()
    if backend == "vm":
        return VmRunner()
    raise ValueError(f"unsupported backend: {backend}")


def _public_compile(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "artifact"}


def _run_single(
    backend: str,
    lang: str,
    code: Path,
    stdin: Path,
    time_limit_s: float,
    memory_mb: int | None,
    kill_factor: float = 3,
) -> dict[str, Any]:
    if (
        not math.isfinite(time_limit_s)
        or not math.isfinite(kill_factor)
        or time_limit_s <= 0
        or kill_factor < 1
    ):
        raise ValueError("time limit must be positive and kill factor must be at least 1")
    if memory_mb is not None and memory_mb < 1:
        raise ValueError("memory limit must be positive")
    runner = _make_runner(backend)
    output_dir = Path(os.environ.get("WATERLOO_JUDGE_WORK", "/srv/waterloo/judge-work")) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    session_dir = Path(tempfile.mkdtemp(prefix="judge-run-", dir=output_dir))
    stdout_dir = output_dir / "stdout"
    stdout_dir.mkdir(exist_ok=True)
    stdout_path = stdout_dir / f"{uuid.uuid4().hex}.bin"
    artifact: Path | None = None
    try:
        compile_result = runner.prepare(lang, code.read_bytes(), session_dir)
        result: dict[str, Any] = {
            "backend": backend,
            "lang": lang,
            "compile": _public_compile(compile_result),
            "verdict": "compile_error",
            "cpu_s": None,
            "wall_s": None,
            "peak_memory_mb": None,
            "stdout_path": None,
            "stdout": "",
            "stdout_truncated": False,
            "stderr_tail": compile_result.get("stderr", "")[-STDERR_TAIL_LIMIT:],
            "over_time": False,
            "over_memory": False,
        }
        if not compile_result["ok"]:
            return result
        artifact = Path(compile_result["artifact"])
        execution = runner.run_artifact(
            lang, artifact, stdin.read_bytes(), time_limit_s, kill_factor
        )
        stdout_path.write_bytes(execution["stdout"])
        time_over = bool(execution.get("over_time", False))
        memory_over = (
            memory_mb is not None
            and execution.get("peak_memory_mb") is not None
            and execution["peak_memory_mb"] > memory_mb
        )
        if execution["killed"]:
            verdict = "killed"
        elif execution["returncode"] != 0:
            verdict = "runtime_error"
        else:
            verdict = "completed"
        result.update({
            "verdict": verdict,
            "cpu_s": execution["cpu_s"],
            "wall_s": execution["wall_s"],
            "peak_memory_mb": execution["peak_memory_mb"],
            "stdout_path": str(stdout_path),
            "stdout": execution["stdout"][:OUTPUT_LIMIT].decode(errors="replace"),
            "stdout_truncated": len(execution["stdout"]) > OUTPUT_LIMIT,
            "stderr_tail": execution["stderr"][-STDERR_TAIL_LIMIT:].decode(errors="replace"),
            "over_time": time_over,
            "over_memory": memory_over,
            "meta": execution["meta"],
        })
        return result
    finally:
        try:
            if artifact is not None:
                runner.release(artifact)
        finally:
            shutil.rmtree(session_dir, ignore_errors=True)


def _grade(
    backend: str,
    code: Path,
    lang: str,
    problem_path: Path,
    tests_path: Path,
    zips_root: Path,
    attempt_id: str,
    out_dir: Path,
    *,
    run_only: bool = False,
    give_up_timeout_s: float = GIVE_UP_TIMEOUT_S,
) -> dict[str, Any]:
    problem = json.loads(problem_path.read_text(encoding="utf-8"))
    cases = _jsonl(tests_path)
    subtasks = {item["name"]: float(item["weight"]) for item in problem.get("subtasks", [])}
    grading_kind = problem.get("grading_kind", "exact")
    shared_tol = problem.get("tolerance")
    if shared_tol is None:
        shared_tol = 1e-6
    abs_tol_value = problem.get("absolute_tolerance")
    rel_tol_value = problem.get("relative_tolerance")
    abs_tol = float(shared_tol if abs_tol_value is None else abs_tol_value)
    rel_tol = float(shared_tol if rel_tol_value is None else rel_tol_value)
    time_limit_s = float(problem["time_limit_s"])
    memory_limit_mb = problem.get("memory_limit_mb")
    if not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError("problem time_limit_s must be a positive finite number")
    if not math.isfinite(give_up_timeout_s) or give_up_timeout_s <= 0:
        raise ValueError("give_up_timeout_s must be a positive finite number")
    isolate_kill_factor = sandbox_timeout_s(time_limit_s, give_up_timeout_s) / time_limit_s
    if memory_limit_mb is not None:
        memory_limit_mb = int(memory_limit_mb)
        if memory_limit_mb < 1:
            raise ValueError("problem memory_limit_mb must be positive")
    checker = None
    if grading_kind in CHECKER_KINDS and not run_only:
        # Lazy: agent.py runs this module as top-level `core` inside the VM, where `.checkers` does not exist.
        from .checkers import load_checker

        checker = load_checker(problem_path.parent)
    runner = _make_runner(backend)
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_dir = out_dir / "stdout"
    stdout_dir.mkdir(exist_ok=True)
    session_dir = Path(tempfile.mkdtemp(prefix="judge-grade-", dir=out_dir))
    try:
        compile_result = runner.prepare(lang, code.read_bytes(), session_dir)
    except Exception:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise
    results: list[TestResult] = []
    run_results: list[dict[str, Any]] = []
    stdout_manifest: list[dict[str, Any]] = []
    passed_by_subtask = {name: True for name in subtasks}
    tested_subtasks: set[str] = set()
    any_over_time = False
    subtask_scores: dict[str, float] = {}
    check_messages: list[dict[str, Any]] = []

    if not compile_result["ok"]:
        for index, case in enumerate(cases, start=1):
            test_id = str(case.get("test_id", f"test-{index:05d}"))
            subtask = str(case["subtask"])
            stdout_manifest.append({"test_id": test_id, "stdout_file": None, "size_bytes": 0})
            if run_only:
                run_results.append({
                    "test_id": test_id, "subtask": subtask, "verdict": "compile_error",
                    "stdout_file": None, "cpu_s": None, "wall_s": None,
                    "peak_memory_mb": None, "over_time": False, "over_memory": False,
                })
                continue
            tested_subtasks.add(subtask)
            passed_by_subtask.setdefault(subtask, True)
            results.append(TestResult(
                attempt_id=attempt_id,
                test_id=test_id,
                subtask=subtask,
                verdict=TestVerdict.COMPILE_ERROR,
                cpu_s=None,
                wall_s=None,
                peak_memory_mb=None,
                over_time=False,
                over_memory=False,
                score=0.0,
            ))
            subtask_scores[subtask] = 0.0
            passed_by_subtask[subtask] = False
        shutil.rmtree(session_dir, ignore_errors=True)
    else:
        artifact = Path(compile_result["artifact"])
        try:
            for index, case in enumerate(cases, start=1):
                test_id = str(case.get("test_id", f"test-{index:05d}"))
                subtask = str(case["subtask"])
                tested_subtasks.add(subtask)
                passed_by_subtask.setdefault(subtask, True)
                input_bytes = _read_path(zips_root, case["input_path"])
                expected = None if run_only else _read_path(zips_root, case["expected_path"])
                execution = runner.run_artifact(
                    lang,
                    artifact,
                    input_bytes,
                    time_limit_s,
                    isolate_kill_factor,
                )
                actual = execution["stdout"]
                test_score = None
                stdout_path = stdout_dir / f"{index:05d}.bin"
                stdout_path.write_bytes(actual)
                stdout_manifest.append({"test_id": test_id, "stdout_file": str(stdout_path),
                                        "size_bytes": len(actual)})
                over_time = bool(execution.get("over_time", False))
                any_over_time |= over_time
                peak_memory = execution.get("peak_memory_mb")
                over_memory = (
                    memory_limit_mb is not None
                    and peak_memory is not None
                    and peak_memory > memory_limit_mb
                )
                if run_only:
                    run_results.append({
                        "test_id": test_id,
                        "subtask": subtask,
                        "verdict": "killed" if execution["killed"] else (
                            "runtime_error" if execution["returncode"] != 0 else "ran"
                        ),
                        "stdout_file": str(stdout_path),
                        "cpu_s": execution.get("cpu_s"),
                        "wall_s": execution.get("wall_s"),
                        "peak_memory_mb": peak_memory,
                        "over_time": over_time,
                        "over_memory": over_memory,
                    })
                    continue
                if execution["killed"]:
                    test_verdict = TestVerdict.KILLED
                elif execution["returncode"] != 0:
                    test_verdict = TestVerdict.RUNTIME_ERROR
                elif checker is not None:
                    from .checkers import run_checker

                    check = run_checker(checker, input_bytes, expected, actual)
                    test_score = check.score
                    test_verdict = TestVerdict.PASS if check.ok else TestVerdict.WRONG_ANSWER
                    check_messages.append({"test_id": test_id, "ok": check.ok, "score": check.score, "message": check.message})
                elif outputs_match(actual, expected, grading_kind, abs_tol, rel_tol):
                    test_verdict = TestVerdict.PASS
                else:
                    test_verdict = TestVerdict.WRONG_ANSWER
                if test_score is None:
                    test_score = 1.0 if test_verdict == TestVerdict.PASS else 0.0
                subtask_scores[subtask] = min(subtask_scores.get(subtask, 1.0), test_score)
                if test_verdict != TestVerdict.PASS:
                    passed_by_subtask[subtask] = False
                results.append(TestResult(
                    attempt_id=attempt_id,
                    test_id=test_id,
                    subtask=subtask,
                    verdict=test_verdict,
                    cpu_s=execution.get("cpu_s"),
                    wall_s=execution.get("wall_s"),
                    peak_memory_mb=peak_memory,
                    over_time=over_time,
                    over_memory=over_memory,
                    score=test_score,
                ))
        finally:
            try:
                runner.release(artifact)
            finally:
                shutil.rmtree(session_dir, ignore_errors=True)

    (out_dir / "stdout_manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in stdout_manifest),
        encoding="utf-8",
    )

    if run_only:
        (out_dir / "run_results.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in run_results),
            encoding="utf-8",
        )
        (out_dir / "compile.json").write_text(
            json.dumps(_public_compile(compile_result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        verdict_counts = {
            verdict: sum(row["verdict"] == verdict for row in run_results)
            for verdict in sorted({row["verdict"] for row in run_results})
        }
        return {
            "attempt_id": attempt_id,
            "problem_id": str(problem["id"]),
            "status": "pending_checker",
            "tests_run": len(run_results),
            "run_verdicts": verdict_counts,
            "env_id": env_id(backend),
        }

    all_pass = bool(results) and all(item.verdict == TestVerdict.PASS for item in results)
    if grading_kind == "scorer":
        partial_score = sum(
            weight * subtask_scores.get(name, 0.0)
            for name, weight in subtasks.items()
            if name in tested_subtasks
        )
    else:
        partial_score = sum(
            weight
            for name, weight in subtasks.items()
            if name in tested_subtasks and passed_by_subtask.get(name, False)
        )
    current_env_id = env_id(backend)
    verdict_record = Verdict(
        attempt_id=attempt_id,
        problem_id=str(problem["id"]),
        subtask_pass=passed_by_subtask,
        all_pass=all_pass,
        partial_score=partial_score,
        outcome=classify_outcome(all_pass, any_over_time),
        env_id=current_env_id,
    )
    with (out_dir / "test_results.jsonl").open("w", encoding="utf-8") as output:
        for item in results:
            output.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
    (out_dir / "verdict.json").write_text(
        json.dumps(asdict(verdict_record), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if checker is not None:
        from .checkers import CHECKER_RUNS_IN

        (out_dir / "checker.json").write_text(
            json.dumps(
                {
                    "grading_kind": grading_kind,
                    "checker_path": str(checker.path),
                    "checker_sha256": checker.sha256,
                    "runs_in": CHECKER_RUNS_IN,
                    "submission_sandbox": backend,
                    "results": check_messages,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    (out_dir / "compile.json").write_text(
        json.dumps(_public_compile(compile_result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "compile": _public_compile(compile_result),
        "verdict": asdict(verdict_record),
        "test_count": len(results),
    }


@contextmanager
def vm_exclusive(backend: str) -> Iterator[float | None]:
    """Hold the judge host's VM lock so one grade or run owns the guest's vCPUs; yields seconds waited."""
    if backend != "vm":
        yield None
        return
    lock_path = Path(os.environ.get("WATERLOO_VM_LOCK", "/srv/waterloo/judge-work/vm.lock"))
    started = time.monotonic()
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield round(time.monotonic() - started, 3)


def run_single(
    backend: str,
    lang: str,
    code: Path,
    stdin: Path,
    time_limit_s: float,
    memory_mb: int | None,
    kill_factor: float = 3,
) -> dict[str, Any]:
    with vm_exclusive(backend) as lock_wait_s:
        result = _run_single(backend, lang, code, stdin, time_limit_s, memory_mb, kill_factor)
    result["vm_lock_wait_s"] = lock_wait_s
    return result


def grade(
    backend: str,
    code: Path,
    lang: str,
    problem_path: Path,
    tests_path: Path,
    zips_root: Path,
    attempt_id: str,
    out_dir: Path,
    *,
    run_only: bool = False,
    give_up_timeout_s: float = GIVE_UP_TIMEOUT_S,
) -> dict[str, Any]:
    with vm_exclusive(backend) as lock_wait_s:
        result = _grade(backend, code, lang, problem_path, tests_path, zips_root, attempt_id, out_dir,
                        run_only=run_only, give_up_timeout_s=give_up_timeout_s)
    (out_dir / "judge.json").write_text(
        json.dumps({
            "backend": backend,
            "vm_lock_wait_s": lock_wait_s,
            "give_up_timeout_s": give_up_timeout_s,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    result["vm_lock_wait_s"] = lock_wait_s
    return result


def env_id(backend: str = "host") -> str:
    if backend == "vm":
        return str(VmRunner().request({"op": "env-id"})["env_id"])
    if backend != "host":
        raise ValueError(f"unsupported backend: {backend}")
    rootfs = Path(os.environ.get("WATERLOO_ROOTFS", "/srv/waterloo/image/mounted-root"))
    if not rootfs.is_mount() or not os.statvfs(rootfs).f_flag & os.ST_RDONLY:
        raise RuntimeError(f"Arch rootfs must be mounted read-only: {rootfs}")
    image_id = rootfs.joinpath("etc/waterloo-env-id").read_text().strip()
    path = Path(os.environ.get("WATERLOO_ENV_ID_FILE", "/srv/waterloo/image/env_id"))
    if not path.is_file():
        raise FileNotFoundError(f"env_id file not found: {path}")
    host_id = path.read_text().strip()
    if host_id != image_id:
        raise RuntimeError("host env_id does not match the mounted Arch image")
    return host_id

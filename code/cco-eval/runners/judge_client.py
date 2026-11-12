"""Clients for the remote Waterloo judge and the local, non-isolated test stub."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from cco_eval.utils import write_json
from cco_eval.waterloo import GIVE_UP_TIMEOUT_S, sandbox_timeout_s

DEFAULT_SSH_HOST = os.environ.get("WATERLOO_SSH_HOST", "user@judge-host.example")
REMOTE_ROOT = os.environ.get("WATERLOO_REMOTE_ROOT", "/srv/waterloo")
DEFAULT_REMOTE_RUNS = f"{REMOTE_ROOT}/runs"
DEFAULT_JUDGE_PATH = f"{REMOTE_ROOT}/src-judge/code/cco-eval/judge"
DEFAULT_ZIPS_ROOT = f"{REMOTE_ROOT}/zips"
CPP_FLAGS = ["-std=gnu++20", "-O2", "-pipe"]
STDOUT_HEAD_CHARS = 2000


def _kill_factor(time_limit_s: float, kill_factor: float | None) -> float:
    if kill_factor is not None:
        return float(kill_factor)
    return sandbox_timeout_s(time_limit_s) / float(time_limit_s)


def _run(command: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command!r}\n{result.stderr[-4000:]}")
    return result


def _last_json(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        try:
            result = json.loads(line)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"judge did not return a JSON object: {text[-2000:]}")


class RemoteJudge:
    def __init__(
        self,
        *,
        ssh_host: str = DEFAULT_SSH_HOST,
        judge_path: str = DEFAULT_JUDGE_PATH,
        remote_runs: str = DEFAULT_REMOTE_RUNS,
        zips_root: str = DEFAULT_ZIPS_ROOT,
        backend: str = "vm",
        ssh_timeout: float = 600,
    ) -> None:
        if backend not in {"vm", "host"}:
            raise ValueError("backend must be vm or host")
        self.ssh_host, self.judge_path = ssh_host, judge_path
        self.remote_runs, self.zips_root, self.ssh_timeout = remote_runs, zips_root, ssh_timeout
        self.backend = backend

    def _ssh(self, script: str, timeout: float | None = None) -> str:
        return _run(["ssh", self.ssh_host, script], timeout=timeout or self.ssh_timeout).stdout

    def _remote_attempt(self, attempt_id: str) -> str:
        safe_id = "".join(char for char in attempt_id if char.isalnum() or char in "-_.")
        if not safe_id or safe_id != attempt_id:
            raise ValueError("attempt ID must contain only letters, digits, dot, dash, or underscore")
        return f"{self.remote_runs.rstrip('/')}/{safe_id}"

    @property
    def _judge_cwd(self) -> str:
        return str(Path(self.judge_path).parent)

    def _prepare(self, attempt_id: str, files: list[Path]) -> str:
        remote_dir = self._remote_attempt(attempt_id)
        self._ssh(f"mkdir -p {shlex.quote(remote_dir)}")
        for source in files:
            if not source.is_file():
                raise FileNotFoundError(source)
            _run(["scp", str(source), f"{self.ssh_host}:{remote_dir}/{source.name}"])
        return remote_dir

    def env_id(self) -> str:
        stdout = self._ssh(
            f"cd {shlex.quote(self._judge_cwd)} && python3 -m judge env-id",
            timeout=self.ssh_timeout,
        )
        try:
            value = _last_json(stdout)
            return str(value.get("env_id", value.get("id", value)))
        except RuntimeError:
            return stdout.strip().splitlines()[-1]

    def run(
        self,
        lang: str,
        code_path: Path,
        stdin_path: Path,
        time_limit_s: float,
        *,
        attempt_id: str,
        memory_mb: int | None = None,
        kill_factor: float | None = None,
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        remote_dir = self._prepare(attempt_id, [code_path, stdin_path])
        code_remote, stdin_remote = f"{remote_dir}/{code_path.name}", f"{remote_dir}/{stdin_path.name}"
        factor = _kill_factor(time_limit_s, kill_factor)
        command = ["python3", "-m", "judge", "run", "--backend", self.backend, "--lang", lang,
                   "--code", code_remote, "--stdin", stdin_remote, "--time-limit", str(time_limit_s),
                   "--kill-factor", str(factor)]
        if memory_mb is not None:
            command += ["--memory-mb", str(memory_mb)]
        script = f"cd {shlex.quote(self._judge_cwd)} && " + " ".join(map(shlex.quote, command))
        result = _last_json(self._ssh(script, timeout=max(self.ssh_timeout, time_limit_s * factor + 30)))
        for key in ("stdout_path",):
            remote_path = result.get(key)
            if isinstance(remote_path, str) and remote_path:
                destination = output_dir or code_path.parent
                destination.mkdir(parents=True, exist_ok=True)
                local_output = destination / f"{attempt_id}-stdout.txt"
                _run(["scp", f"{self.ssh_host}:{remote_path}", str(local_output)])
                result[key] = str(local_output)
                result["stdout_head"] = local_output.read_text(encoding="utf-8", errors="replace")[:STDOUT_HEAD_CHARS]
        return result

    def grade(
        self, *, attempt_id: str, code_path: Path, lang: str, problem_json: Path,
        tests_jsonl: Path, out_dir: Path, run_only: bool = False,
    ) -> dict[str, Any]:
        checker = problem_json.parent / "checker.py"
        remote_dir = self._prepare(attempt_id, [code_path, problem_json, tests_jsonl,
                                                *([checker] if checker.is_file() else [])])
        remote_out = f"{remote_dir}/grade"
        local_testdata = tests_jsonl.parent / "test-data"
        if local_testdata.is_dir():
            _run(["scp", "-r", str(local_testdata), f"{self.ssh_host}:{remote_dir}/test-data"])
        self._ssh(f"mkdir -p {shlex.quote(remote_out)}")
        command = ["python3", "-m", "judge", "grade", "--code", f"{remote_dir}/{code_path.name}",
                   "--lang", lang, "--problem", f"{remote_dir}/{problem_json.name}", "--tests",
                   f"{remote_dir}/{tests_jsonl.name}", "--zips-root", self.zips_root,
                   "--attempt-id", attempt_id, "--out", remote_out,
                   "--give-up-timeout", str(GIVE_UP_TIMEOUT_S)]
        if run_only:
            command.append("--run-only")
        script = f"cd {shlex.quote(self._judge_cwd)} && " + " ".join(map(shlex.quote, command))
        stdout = self._ssh(script, timeout=max(self.ssh_timeout, GIVE_UP_TIMEOUT_S * 150 + 600))
        out_dir.mkdir(parents=True, exist_ok=True)
        if run_only:
            _run(["scp", f"{self.ssh_host}:{remote_out}/run_results.jsonl", str(out_dir / "run_results.jsonl")])
            _run(["scp", f"{self.ssh_host}:{remote_out}/compile.json", str(out_dir / "compile.json")])
            _run(["scp", f"{self.ssh_host}:{remote_out}/stdout_manifest.jsonl", str(out_dir / "stdout_manifest.jsonl")])
            _run(["scp", f"{self.ssh_host}:{remote_out}/judge.json", str(out_dir / "judge.json")])
            return _last_json(stdout)
        for name in ("test_results.jsonl", "verdict.json", "compile.json", "stdout_manifest.jsonl", "judge.json"):
            _run(["scp", f"{self.ssh_host}:{remote_out}/{name}", str(out_dir / name)])
        verdict_path = out_dir / "verdict.json"
        if verdict_path.is_file():
            return json.loads(verdict_path.read_text(encoding="utf-8"))
        return _last_json(stdout)


class StubJudge:
    """Local subprocess judge for tests and smoke runs; it provides no isolation."""

    def __init__(self, env_id: str = "stub-local-unisolated-v1") -> None:
        self._env_id = env_id

    def env_id(self) -> str:
        return self._env_id

    def run(
        self, lang: str, code_path: Path, stdin_path: Path, time_limit_s: float,
        *, attempt_id: str = "stub", memory_mb: int | None = None, kill_factor: float | None = None,
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        workdir = output_dir or code_path.parent
        workdir.mkdir(parents=True, exist_ok=True)
        executable: Path | None = None
        compile_record: dict[str, Any] = {"ok": True, "stderr_tail": ""}
        if lang == "cpp":
            executable = workdir / f"{code_path.stem}.bin"
            compile_result = subprocess.run(["g++", *CPP_FLAGS, str(code_path), "-o", str(executable)],
                                             capture_output=True, text=True)
            compile_record = {"ok": compile_result.returncode == 0, "stderr_tail": compile_result.stderr[-4000:]}
            if compile_result.returncode:
                return {"compile": compile_record, "verdict": "compile_error", "cpu_s": None,
                        "wall_s": None, "peak_memory_mb": None, "stdout_path": None,
                        "stdout_head": "", "stderr_tail": compile_result.stderr[-4000:],
                        "over_time": False, "over_memory": False}
        argv = [str(executable)] if executable else ["python3", str(code_path)]
        started = time.perf_counter()
        output_path = workdir / f"{attempt_id}-run.stdout"
        timeout_s = time_limit_s * _kill_factor(time_limit_s, kill_factor)
        try:
            with stdin_path.open("rb") as stdin, output_path.open("wb") as stdout:
                proc = subprocess.run(argv, cwd=workdir, stdin=stdin, stdout=stdout,
                                      stderr=subprocess.PIPE, timeout=timeout_s)
            stderr = proc.stderr.decode("utf-8", errors="replace")
            elapsed = time.perf_counter() - started
            timed_out = False
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            timed_out, returncode = True, None
        stdout_text = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
        over_time = elapsed > time_limit_s
        verdict = "killed" if timed_out else "runtime_error" if returncode else "ran"
        return {"compile": compile_record, "verdict": verdict, "cpu_s": None,
                "wall_s": round(elapsed, 6), "peak_memory_mb": None,
                "stdout_path": str(output_path), "stdout_head": stdout_text[:STDOUT_HEAD_CHARS],
                "stderr_tail": stderr[-4000:], "over_time": over_time,
                "over_memory": False}

    def grade(
        self, *, attempt_id: str, code_path: Path, lang: str, problem_json: Path,
        tests_jsonl: Path, out_dir: Path, run_only: bool = False,
    ) -> dict[str, Any]:
        problem = json.loads(problem_json.read_text(encoding="utf-8"))
        time_limit = float(problem.get("time_limit_s", 2))
        memory_limit = problem.get("memory_limit_mb")
        tests = [json.loads(line) for line in tests_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
        if run_only:
            out_dir.mkdir(parents=True, exist_ok=True)
            records: list[dict[str, Any]] = []
            for index, test in enumerate(tests):
                base = tests_jsonl.parent
                input_path = Path(str(test.get("input_path", test.get("input", ""))))
                if not input_path.is_absolute():
                    input_path = base / input_path
                with tempfile.TemporaryDirectory(prefix="waterloo-stub-") as temp:
                    stdin = Path(temp) / "stdin.txt"
                    shutil.copyfile(input_path, stdin)
                    result = self.run(lang, code_path, stdin, time_limit,
                                      attempt_id=f"{attempt_id}-{index}", memory_mb=memory_limit)
                    records.append({
                        "test_id": str(test.get("id", index)),
                        "subtask": str(test.get("subtask", "all")),
                        "verdict": "killed" if result["verdict"] == "killed" else (
                            "runtime_error" if result["verdict"] == "runtime_error" else (
                                "compile_error" if not result.get("compile", {}).get("ok") else "ran"
                            )
                        ),
                        "stdout_file": result.get("stdout_path"),
                        "cpu_s": result.get("cpu_s"), "wall_s": result.get("wall_s"),
                        "peak_memory_mb": result.get("peak_memory_mb"),
                        "over_time": bool(result.get("over_time")),
                        "over_memory": bool(result.get("over_memory")),
                    })
            (out_dir / "run_results.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in records), encoding="utf-8")
            counts = {verdict: sum(row["verdict"] == verdict for row in records)
                      for verdict in sorted({row["verdict"] for row in records})}
            return {"attempt_id": attempt_id, "problem_id": str(problem.get("id", "unknown")),
                    "status": "pending_checker", "tests_run": len(records), "run_verdicts": counts,
                    "env_id": self._env_id}
        records: list[dict[str, Any]] = []
        for index, test in enumerate(tests):
            base = tests_jsonl.parent
            input_path = Path(str(test.get("input_path", test.get("input", ""))))
            expected_path = Path(str(test.get("expected_path", test.get("output_path", test.get("expected", test.get("output", ""))))))
            if not input_path.is_absolute(): input_path = base / input_path
            if not expected_path.is_absolute(): expected_path = base / expected_path
            with tempfile.TemporaryDirectory(prefix="waterloo-stub-") as temp:
                stdin = Path(temp) / "stdin.txt"
                shutil.copyfile(input_path, stdin)
                result = self.run(lang, code_path, stdin, time_limit, attempt_id=f"{attempt_id}-{index}", memory_mb=memory_limit,
                                  output_dir=Path(temp))
                stdout_path = Path(str(result["stdout_path"])) if result.get("stdout_path") else None
                got = stdout_path.read_bytes() if stdout_path and stdout_path.exists() else b""
                want = expected_path.read_bytes()
            passed = result.get("verdict") == "ran" and got.strip() == want.strip()
            case_verdict = "pass" if passed else "killed" if result.get("verdict") == "killed" else "compile_error" if not result.get("compile", {}).get("ok") else "runtime_error" if result.get("verdict") == "runtime_error" else "wrong_answer"
            records.append({"attempt_id": attempt_id, "test_id": str(test.get("id", index)),
                            "subtask": str(test.get("subtask", "all")), "verdict": case_verdict,
                            "cpu_s": result.get("cpu_s"), "wall_s": result.get("wall_s"),
                            "peak_memory_mb": result.get("peak_memory_mb"),
                            "over_time": bool(result.get("over_time")),
                            "over_memory": bool(result.get("over_memory"))})
        out_dir.mkdir(parents=True, exist_ok=True)
        tests_out = out_dir / "test_results.jsonl"
        tests_out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")
        passed = bool(records) and all(record["verdict"] == "pass" for record in records)
        over_time_pass = passed and any(record["over_time"] for record in records)
        verdict = {"attempt_id": attempt_id, "problem_id": str(problem.get("id", "unknown")),
                   "subtask_pass": {"all": passed}, "all_pass": passed,
                   "partial_score": 1.0 if passed else 0.0,
                   "outcome": "pass_over_time" if over_time_pass else "pass_on_time" if passed else "fail",
                   "env_id": self._env_id}
        write_json(out_dir / "verdict.json", verdict)
        return verdict

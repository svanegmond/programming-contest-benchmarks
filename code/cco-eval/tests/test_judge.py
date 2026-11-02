import fcntl
import json
import shutil
import socket
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from judge import core
from judge.cli import _parser
from judge.core import classify_outcome, outputs_match


def test_float_tolerance_accepts_reformatted_numeric_tokens():
    assert outputs_match(b"1.0000000 2e0\n", b"1 2.000000", "float_tol")


def test_float_tolerance_rejects_difference_past_tolerance():
    assert not outputs_match(b"1.00001", b"1", "float_tol", 1e-6, 1e-6)


def test_float_tolerance_keeps_nonnumeric_tokens_exact():
    assert not outputs_match(b"answer 1", b"answers 1.0", "float_tol")
    assert outputs_match(b"\xff", b"\xff", "float_tol")
    assert not outputs_match(b"\xff", b"\xfe", "float_tol")


def test_exact_mode_matches_code_runner_whitespace_behavior():
    assert outputs_match(b"a\t b\r\n c", b"a b\nc\n", "exact")
    assert not outputs_match(b"a bc", b"a b c", "exact")


def test_outcome3_classification():
    from cco_eval.waterloo import Outcome3

    assert classify_outcome(True, False) == Outcome3.PASS_ON_TIME
    assert classify_outcome(True, True) == Outcome3.PASS_OVER_TIME
    assert classify_outcome(False, False) == Outcome3.FAIL
    assert classify_outcome(False, True) == Outcome3.FAIL


def test_backend_option_works_before_or_after_command():
    before = _parser().parse_args(["--backend", "host", "env-id"])
    after = _parser().parse_args(["env-id", "--backend", "host"])
    assert before.backend == after.backend == "host"


def test_grade_uses_test_data_inside_zip_members(tmp_path, monkeypatch):
    class Runner:
        def prepare(self, lang, source, workdir):
            artifact = workdir / "compiled"
            artifact.write_bytes(source)
            return {"ok": True, "artifact": str(artifact)}

        def run_artifact(self, lang, artifact, stdin, time_limit_s, kill_factor):
            return {
                "stdout": stdin.upper(),
                "stderr": b"",
                "returncode": 0,
                "cpu_s": 0.01,
                "wall_s": 0.02,
                "peak_memory_mb": 1.0,
                "killed": False,
                "over_time": False,
                "meta": {},
            }

        def release(self, artifact):
            pass

    with zipfile.ZipFile(tmp_path / "cases.zip", "w") as archive:
        archive.writestr("nested/input.txt", "sample input\n")
        archive.writestr("nested/expected.txt", "SAMPLE INPUT\n")

    code = tmp_path / "submission.py"
    code.write_text("print('sample input')\n")
    problem = tmp_path / "problem.json"
    problem.write_text(json.dumps({
        "id": "cco-test",
        "time_limit_s": 1,
        "memory_limit_mb": None,
        "grading_kind": "exact",
        "subtasks": [{"name": "main", "weight": 1.0}],
    }))
    tests = tmp_path / "tests.jsonl"
    tests.write_text(json.dumps({
        "problem_id": "cco-test",
        "subtask": "main",
        "input_path": "cases.zip::nested/input.txt",
        "expected_path": "cases.zip::nested/expected.txt",
    }) + "\n")
    monkeypatch.setattr(core, "_make_runner", lambda backend: Runner())
    monkeypatch.setattr(core, "env_id", lambda backend="host": "test-env")

    result = core.grade(
        "host", code, "python", problem, tests, tmp_path, "attempt-1", tmp_path / "out"
    )

    assert result["verdict"]["outcome"] == "pass_on_time"
    assert result["verdict"]["partial_score"] == 1.0
    test_result = json.loads((tmp_path / "out/test_results.jsonl").read_text())
    assert test_result["verdict"] == "pass"


def test_run_only_does_not_read_expected_outputs_or_write_a_verdict(tmp_path, monkeypatch):
    class Runner:
        def prepare(self, lang, source, workdir):
            artifact = workdir / "compiled"
            artifact.write_bytes(source)
            return {"ok": True, "artifact": str(artifact)}

        def run_artifact(self, lang, artifact, stdin, time_limit_s, kill_factor):
            return {"stdout": b"generated output\n", "stderr": b"", "returncode": 0,
                    "cpu_s": 0.01, "wall_s": 0.02, "peak_memory_mb": 1.0,
                    "killed": False, "over_time": False, "meta": {}}

        def release(self, artifact):
            pass

    with zipfile.ZipFile(tmp_path / "cases.zip", "w") as archive:
        archive.writestr("input.txt", "sample input\n")
    code = tmp_path / "submission.py"
    code.write_text("print('generated output')\n")
    problem = tmp_path / "problem.json"
    problem.write_text(json.dumps({"id": "cco-checker", "time_limit_s": 1,
                                   "grading_kind": "validator", "subtasks": []}))
    tests = tmp_path / "tests.jsonl"
    tests.write_text(json.dumps({"subtask": "all", "input_path": "cases.zip::input.txt",
                                 "expected_path": "cases.zip::missing-output.txt"}) + "\n")
    monkeypatch.setattr(core, "_make_runner", lambda backend: Runner())
    monkeypatch.setattr(core, "env_id", lambda backend="host": "test-env")
    monkeypatch.setattr(core, "outputs_match", lambda *args: (_ for _ in ()).throw(AssertionError("diff grading ran")))

    result = core.grade("host", code, "python", problem, tests, tmp_path, "checker-1",
                        tmp_path / "out", run_only=True)

    assert result["status"] == "pending_checker" and result["tests_run"] == 1
    assert not (tmp_path / "out/verdict.json").exists()
    assert not (tmp_path / "out/test_results.jsonl").exists()
    run_result = json.loads((tmp_path / "out/run_results.jsonl").read_text())
    assert run_result["verdict"] == "ran"
    assert Path(run_result["stdout_file"]).read_bytes() == b"generated output\n"
    manifest = json.loads((tmp_path / "out/stdout_manifest.jsonl").read_text())
    assert manifest["size_bytes"] == len(b"generated output\n")


def test_vm_request_survives_replies_that_arrive_in_one_read():
    # A second judge holding QEMU's single control connection makes this client's pings
    # queue up; the guest then answers them back to back and they arrive in one recv.
    pong = b'{"ok":true,"pong":true}\n'
    socket_dir = tempfile.mkdtemp(prefix="vm")
    socket_path = Path(socket_dir) / "control.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def guest():
        connection, _ = server.accept()
        with connection, connection.makefile("rb") as lines:
            lines.readline()
            connection.sendall(pong + pong)
            assert json.loads(lines.readline()) == {"op": "env-id"}
            connection.sendall(pong + b'{"ok":true,"env_id":"abc"}\n')

    thread = threading.Thread(target=guest)
    thread.start()
    try:
        response = core.VmRunner(socket_path).request({"op": "env-id"})
    finally:
        thread.join(timeout=5)
        server.close()
        shutil.rmtree(socket_dir)

    assert response == {"ok": True, "env_id": "abc"}


def _vm_grade_inputs(tmp_path):
    with zipfile.ZipFile(tmp_path / "cases.zip", "w") as archive:
        archive.writestr("input.txt", "x\n")
        archive.writestr("expected.txt", "x\n")
    code = tmp_path / "submission.py"
    code.write_text("print('x')\n")
    problem = tmp_path / "problem.json"
    problem.write_text(json.dumps({"id": "cco-lock", "time_limit_s": 1, "grading_kind": "exact",
                                   "subtasks": [{"name": "main", "weight": 1.0}]}))
    tests = tmp_path / "tests.jsonl"
    tests.write_text(json.dumps({"subtask": "main", "input_path": "cases.zip::input.txt",
                                 "expected_path": "cases.zip::expected.txt"}) + "\n")
    return code, problem, tests


class _TimedRunner:
    def __init__(self, events):
        self.events = events

    def prepare(self, lang, source, workdir):
        self.events.append(("start", time.monotonic()))
        return {"ok": True, "artifact": str(workdir / "compiled")}

    def run_artifact(self, lang, artifact, stdin, time_limit_s, kill_factor):
        time.sleep(0.2)
        return {"returncode": 0, "stdout": stdin, "stderr": b"", "cpu_s": 0.01, "wall_s": 0.01,
                "peak_memory_mb": 1.0, "killed": False, "over_time": False, "meta": {}}

    def release(self, artifact):
        self.events.append(("end", time.monotonic()))


def test_concurrent_vm_grades_run_one_after_the_other_and_record_the_wait(tmp_path, monkeypatch):
    events = []
    monkeypatch.setenv("WATERLOO_VM_LOCK", str(tmp_path / "vm.lock"))
    monkeypatch.setattr(core, "_make_runner", lambda backend: _TimedRunner(events))
    monkeypatch.setattr(core, "env_id", lambda backend="host": "test-env")
    code, problem, tests = _vm_grade_inputs(tmp_path)

    def grade(name):
        core.grade("vm", code, "python", problem, tests, tmp_path, name, tmp_path / name)

    threads = [threading.Thread(target=grade, args=(f"attempt-{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert [kind for kind, _ in sorted(events, key=lambda event: event[1])] == ["start", "end"] * 3
    waits = sorted(json.loads((tmp_path / f"attempt-{i}/judge.json").read_text())["vm_lock_wait_s"]
                   for i in range(3))
    assert waits[0] < 0.1 and waits[1] >= 0.15 and waits[2] >= 0.35


def test_vm_grade_waits_for_a_lock_held_by_another_process_and_host_grade_does_not(tmp_path, monkeypatch):
    events = []
    lock_path = tmp_path / "vm.lock"
    monkeypatch.setenv("WATERLOO_VM_LOCK", str(lock_path))
    monkeypatch.setattr(core, "_make_runner", lambda backend: _TimedRunner(events))
    monkeypatch.setattr(core, "env_id", lambda backend="host": "test-env")
    code, problem, tests = _vm_grade_inputs(tmp_path)
    holder = lock_path.open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        host = core.grade("host", code, "python", problem, tests, tmp_path, "host-1", tmp_path / "host")
        assert host["vm_lock_wait_s"] is None
        thread = threading.Thread(target=core.grade, args=("vm", code, "python", problem, tests, tmp_path,
                                                             "vm-1", tmp_path / "vm"))
        thread.start()
        time.sleep(0.4)
        assert len(events) == 2  # only the host grade has run
    finally:
        holder.close()
    thread.join(timeout=10)

    assert len(events) == 4
    assert json.loads((tmp_path / "vm/judge.json").read_text())["vm_lock_wait_s"] >= 0.35

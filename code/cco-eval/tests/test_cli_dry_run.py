"""CLI integration tests — dry-run and help flag tests."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Run from the repository root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CCO_EVAL_SCRIPT = Path(__file__).resolve().parent.parent / "cco_eval.py"
CCO_CORPUS = PROJECT_ROOT / "statements"


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CCO_EVAL_SCRIPT)] + args,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        **kwargs,
    )


# --- run --help ---

def test_run_help():
    result = _run(["run", "--help"])
    assert result.returncode == 0
    for flag in ["--corpus", "--model", "--problem", "--out", "--dry-run", "--provider",
                 "--ollama-url", "--max-tokens", "--compile-timeout", "--test-timeout"]:
        assert flag in result.stdout, f"missing flag: {flag}"


# --- run --dry-run CCO pipeline ---

def test_run_dry_run_cco():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run([
            "run",
            "--corpus", str(CCO_CORPUS),
            "--model", "t",
            "--problem", "2024-p1",
            "--out", tmpdir,
            "--dry-run",
        ])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        results_path = Path(tmpdir) / "results.jsonl"
        assert results_path.exists()
        record = json.loads(results_path.read_text().strip().split("\n")[-1])
        assert record["adapter"] == "cco"
        assert record["stages"]["generate"]["ok"] is True
        assert record["score"]["compile_success"] is True


# --- index ---

def test_index_command():
    with tempfile.TemporaryDirectory() as tmpdir:
        index_path = Path(tmpdir) / "test_index.json"
        result = _run([
            "index",
            "--corpus", str(CCO_CORPUS),
            "--index-path", str(index_path),
        ])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert index["summary"]["problems"] > 0


# --- list ---

def test_list_command():
    result = _run([
        "list",
        "--corpus", str(CCO_CORPUS),
    ])
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "2024-p1" in result.stdout



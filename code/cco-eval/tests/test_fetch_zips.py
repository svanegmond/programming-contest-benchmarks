"""Tests for CCO ZIP fetching."""

from __future__ import annotations

import io
import json
from pathlib import Path

import fetch_zips


def write_manifest(path: Path, content: bytes) -> None:
    path.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "kind": "test_data_zip",
                        "year": "2024",
                        "url": "https://example.test/2024/contest.zip",
                        "bytes": len(content),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def run_fetch(monkeypatch, manifest: Path, output_dir: Path, *extra: str) -> int:
    monkeypatch.setattr(
        "sys.argv",
        ["fetch_zips.py", "--manifest", str(manifest), "--output-dir", str(output_dir), *extra],
    )
    return fetch_zips.main()


def test_fetch_downloads_zip_and_skips_existing_file(tmp_path, monkeypatch):
    content = b"official zip bytes"
    manifest = tmp_path / "manifest.json"
    output_dir = tmp_path / "zips"
    write_manifest(manifest, content)
    calls = 0

    def open_url(request, timeout):
        nonlocal calls
        calls += 1
        return io.BytesIO(content)

    monkeypatch.setattr(fetch_zips, "urlopen", open_url)
    assert run_fetch(monkeypatch, manifest, output_dir) == 0
    assert (output_dir / "2024" / "contest.zip").read_bytes() == content

    def unexpected_download(request, timeout):
        raise AssertionError("existing ZIP should be reused")

    monkeypatch.setattr(fetch_zips, "urlopen", unexpected_download)
    assert run_fetch(monkeypatch, manifest, output_dir) == 0
    assert calls == 1


def test_size_mismatch_warns_and_keeps_file(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "manifest.json"
    output_dir = tmp_path / "zips"
    write_manifest(manifest, b"captured bytes")

    monkeypatch.setattr(fetch_zips, "urlopen", lambda request, timeout: io.BytesIO(b"changed official bytes"))
    assert run_fetch(monkeypatch, manifest, output_dir) == 0
    assert (output_dir / "2024" / "contest.zip").read_bytes() == b"changed official bytes"
    assert "manifest lists 14" in capsys.readouterr().err


def test_dry_run_downloads_nothing(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    output_dir = tmp_path / "zips"
    write_manifest(manifest, b"x")

    def forbidden(request, timeout):
        raise AssertionError("dry run must not download")

    monkeypatch.setattr(fetch_zips, "urlopen", forbidden)
    assert run_fetch(monkeypatch, manifest, output_dir, "--dry-run") == 0
    assert not output_dir.exists()

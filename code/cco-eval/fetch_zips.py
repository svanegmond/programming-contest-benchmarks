#!/usr/bin/env python3
"""Fetch the official CCO test-data ZIPs listed in testdata/zips.json from their CEMC URLs.

CEMC does not publish checksums, so files are not verified beyond a size check
against the byte count recorded in the manifest (a mismatch prints a warning).
--dry-run downloads nothing: it reports which ZIPs are present and which would be fetched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]


def download(url: str, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    size = 0
    request = Request(url, headers={"User-Agent": "cco-eval-fetch-zips/1.0"})
    try:
        with urlopen(request, timeout=180) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                size += len(chunk)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return size


def manifest_zips(manifest_path: Path) -> list[dict[str, object]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = [item for item in manifest["resources"] if item.get("kind") == "test_data_zip"]
    for item in rows:
        for field in ("url", "year"):
            if not item.get(field):
                raise ValueError(f"ZIP manifest row missing {field}: {item}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "testdata/zips.json")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "testdata/zips")
    parser.add_argument("--dry-run", action="store_true", help="Download nothing; report what is present.")
    args = parser.parse_args()

    failures = 0
    rows = manifest_zips(args.manifest)
    for row in rows:
        year = str(row["year"])
        url = str(row["url"])
        filename = Path(url.split("?", 1)[0]).name
        if not filename or filename in {".", ".."}:
            raise ValueError(f"Cannot derive ZIP filename from URL: {url}")

        target = args.output_dir / year / filename
        if args.dry_run:
            if target.is_file():
                print(f"present {year}/{filename} ({target.stat().st_size} bytes)")
            else:
                print(f"would fetch {year}/{filename} ({row.get('bytes')} bytes) from {url}")
            continue
        if target.is_file():
            size = target.stat().st_size
            print(f"exists {year}/{filename}")
        else:
            try:
                size = download(url, target)
            except Exception as exc:
                failures += 1
                print(f"download failed for {url}: {exc}", file=sys.stderr)
                continue
            print(f"fetched {year}/{filename} ({size} bytes)")
        expected = row.get("bytes")
        if expected is not None and size != expected:
            print(f"warning: {year}/{filename} is {size} bytes, manifest lists {expected}", file=sys.stderr)

    print(f"{len(rows)} manifest ZIP rows, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

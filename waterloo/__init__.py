"""One-command entry point for the existing Waterloo harness."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code/cco-eval"))

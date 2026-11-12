"""Published reports live on GitHub Pages, not in the git tree on main."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_main_does_not_commit_run_outputs_or_pilot_reports():
    assert not (ROOT / "runs").exists() or not any((ROOT / "runs").iterdir())
    assert not (ROOT / "reports" / "pilot2.md").exists()
    assert not (ROOT / "reports" / "pilotfix.md").exists()

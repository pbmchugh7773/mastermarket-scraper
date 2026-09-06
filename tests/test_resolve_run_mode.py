"""
Tests for scripts/resolve_run_mode.sh — decides promotions vs retry mode for
daily-scraping.yml from the cron expression that fired (github.event.schedule)
instead of the wall clock. GitHub delayed the Mon/Thu crons by 4–11 h in late
Aug 2026, so any hour/day-of-week check on `date` picks the wrong mode.

Usage under test: resolve_run_mode.sh <event_name> <schedule> <manual_promotions>
Prints exactly one word on stdout: "promotions" or "retry".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "resolve_run_mode.sh"


def _run(*args: str):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


@pytest.mark.parametrize("event, schedule, manual, expected", [
    ("schedule", "17 8 * * 0", "false", "promotions"),   # Sunday cron → promotions
    ("schedule", "0 8 * * 0", "false", "promotions"),    # old minute-00 Sunday cron still works
    ("schedule", "17 4 * * 1,4", "false", "retry"),      # Mon/Thu batch 1
    ("schedule", "17 6 * * 1,4", "false", "retry"),      # batch 2
    ("schedule", "17 8 * * 1,4", "false", "retry"),      # batch 3 — same hour as Sunday, different dow
    ("workflow_dispatch", "", "true", "promotions"),     # manual promotions
    ("workflow_dispatch", "", "false", "retry"),         # manual default
    ("workflow_dispatch", "", "", "retry"),              # manual, input unset
    ("schedule", "", "false", "retry"),                  # schedule missing → safe default
])
def test_mode_resolution(event, schedule, manual, expected):
    proc = _run(event, schedule, manual)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_manual_promotions_wins_over_schedule():
    proc = _run("workflow_dispatch", "17 4 * * 1,4", "true")
    assert proc.stdout.strip() == "promotions"


def test_missing_event_name_is_usage_error():
    proc = _run()
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()

"""
Tests for scripts/notify_failure.sh — turns a failed scraper run into a
GitHub issue (label scraper-alert). One open issue per workflow: a new failure
adds a comment to the existing issue instead of opening another, so a week of
red runs is one thread, not seven.

`gh` is replaced by a stub on PATH that records every invocation.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "notify_failure.sh"

GH_STUB = r'''#!/usr/bin/env bash
# Records args; answers `issue list` with $GH_EXISTING (empty = none open).
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "issue list") printf '%s' "${GH_EXISTING:-}" ;;
  "issue create") echo "https://github.com/o/r/issues/99" ;;
  *) : ;;
esac
'''


@pytest.fixture
def gh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("GH_REPO", "o/r")
    return log


def _run(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


def test_creates_issue_when_none_open(gh, monkeypatch):
    monkeypatch.setenv("GH_EXISTING", "")
    proc = _run("Price Scraping (Mon/Thu)", "https://github.com/o/r/actions/runs/1", "Aldi: silent-ingestion alarm")
    assert proc.returncode == 0, proc.stderr
    log = gh.read_text()
    assert "label create scraper-alert" in log
    assert "issue create" in log
    assert "[scraper-alert] Price Scraping (Mon/Thu)" in log
    assert "--label scraper-alert" in log
    assert "https://github.com/o/r/actions/runs/1" in log
    assert "Aldi: silent-ingestion alarm" in log
    assert "issue comment" not in log


def test_comments_on_existing_open_issue(gh, monkeypatch):
    monkeypatch.setenv("GH_EXISTING", "42")
    proc = _run("Apify Tesco Scraper", "https://github.com/o/r/actions/runs/2")
    assert proc.returncode == 0, proc.stderr
    log = gh.read_text()
    assert "issue comment 42" in log
    assert "https://github.com/o/r/actions/runs/2" in log
    assert "issue create" not in log


def test_missing_args_is_usage_error(gh):
    proc = _run("only-one-arg")
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()

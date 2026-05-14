"""Synthetic log tests for scripts/validate_scrape_log.sh (MASA-159).

These tests defend against the 2026-04-29 regex regression where a leading
anchor on the "Processed:"/"Uploaded:" patterns failed against Python-logger-
prefixed lines, silently classifying every retry-mode run as 0/0.

We cover the three log shapes the validator is required to classify
correctly per the MASA-159 DoD:

  (a) healthy retry-mode log         → exit 0
  (b) real auth-fail log             → exit 1 (auth-fail branch)
  (c) silent-outage log (700/0)      → exit 1 (silent-ingestion branch)

Plus a few smaller cases for coverage of MIN_PROCESSED gating and the
JWT-thrash branch.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "scripts" / "validate_scrape_log.sh"

# All log fixtures use the real `timestamp - LEVEL - message` shape produced by
# logging.basicConfig in simple_local_to_prod.py — the regex fix that landed in
# MASA-108's tuning depends on having no leading anchor on "Processed:" /
# "Uploaded:" lines.
LINE_PREFIX = "2026-04-30 06:12:18,442 - INFO -"


def _run_validator(tmp_path: Path, log_body: str, run_type: str, store: str = "Lidl"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / f"scrape_{store}.log"
    log_path.write_text(log_body, encoding="utf-8")
    return subprocess.run(
        ["bash", str(VALIDATOR), str(log_path), run_type, store],
        capture_output=True,
        text=True,
    )


def _summary(processed: int, uploaded: int) -> str:
    """End-of-run summary block emitted by simple_local_to_prod.py."""
    return (
        f"{LINE_PREFIX} 📊 Final summary:\n"
        f"{LINE_PREFIX}   Store: Lidl\n"
        f"{LINE_PREFIX}   Processed: {processed}\n"
        f"{LINE_PREFIX}   Uploaded: {uploaded}\n"
        f"{LINE_PREFIX}   Failed: {max(processed - uploaded, 0)}\n"
    )


@pytest.fixture(scope="module", autouse=True)
def _require_bash():
    if shutil.which("bash") is None:
        pytest.skip("bash not available — validator script requires bash")
    assert VALIDATOR.exists(), f"validator script missing at {VALIDATOR}"


# ---------------------------------------------------------------------------
# (a) Healthy retry-mode log — 3 processed, 0 uploaded. PROCESSED < 50 so the
#     upload-percent check is skipped. No auth failures. Validator MUST pass.
# ---------------------------------------------------------------------------
def test_healthy_retry_mode_noop_passes(tmp_path: Path):
    log = (
        f"{LINE_PREFIX} 🔑 Authentication successful\n"
        f"{LINE_PREFIX} 🔄 RETRY MODE: processing stragglers only\n"
        f"{LINE_PREFIX} 🛒 Scraping product 1/3\n"
        f"{LINE_PREFIX} ⚠️ Product 1 unavailable (404 stale URL)\n"
        f"{LINE_PREFIX} ⚠️ Product 2 unavailable (404 stale URL)\n"
        f"{LINE_PREFIX} ⚠️ Product 3 unavailable (404 stale URL)\n"
        + _summary(processed=3, uploaded=0)
    )

    result = _run_validator(tmp_path, log, run_type="retry")

    assert result.returncode == 0, (
        f"healthy retry-mode no-op should pass; stdout=\n{result.stdout}\nstderr=\n{result.stderr}"
    )
    assert "skipping upload-percent check" in result.stdout
    assert "processed=3" in result.stdout
    assert "uploads=0" in result.stdout


def test_healthy_retry_mode_with_uploads_passes(tmp_path: Path):
    """A retry-mode batch that did upload everything it processed must pass."""
    log = (
        f"{LINE_PREFIX} 🔑 Authentication successful\n"
        + _summary(processed=12, uploaded=11)
    )

    result = _run_validator(tmp_path, log, run_type="retry")

    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# (b) Real auth-fail log — credential outage. UPLOADS could be 0 because we
#     never authenticated. The auth-fail branch fires regardless of PROCESSED.
# ---------------------------------------------------------------------------
def test_auth_fail_log_fails(tmp_path: Path):
    log = (
        f"{LINE_PREFIX} 🔑 Attempting login as scraper@mastermarket\n"
        f"{LINE_PREFIX} ❌ Authentication failed: 401 Unauthorized\n"
        f"{LINE_PREFIX} ❌ Authentication error: cannot continue without JWT\n"
        + _summary(processed=0, uploaded=0)
    )

    result = _run_validator(tmp_path, log, run_type="retry")

    assert result.returncode == 1
    assert "fatal auth failures detected" in result.stdout
    # Auth-fail must trip even in retry-mode with PROCESSED=0 — credential
    # outages are always loud.
    assert "auth_fails=2" in result.stdout


def test_auth_fail_in_main_batch_fails(tmp_path: Path):
    """Auth-fail also trips on main batches (promotions / manual / non-retry)."""
    log = (
        f"{LINE_PREFIX} ❌ Authentication failed: 401 Unauthorized\n"
        + _summary(processed=0, uploaded=0)
    )

    result = _run_validator(tmp_path, log, run_type="promotions")

    assert result.returncode == 1
    assert "fatal auth failures detected" in result.stdout


# ---------------------------------------------------------------------------
# (c) Silent-outage log — looks like a normal 4AM main batch but every upload
#     failed silently (MASA-106 shape: 700 processed, 0 uploaded). The
#     upload-percent branch MUST fire because PROCESSED >= MIN_PROCESSED=50.
# ---------------------------------------------------------------------------
def test_silent_outage_main_batch_fails(tmp_path: Path):
    log = (
        f"{LINE_PREFIX} 🔑 Authentication successful\n"
        f"{LINE_PREFIX} 🛒 Scraping 700 products\n"
        f"{LINE_PREFIX} ⚠️ Upload failed: 401 (post-MASA-106 retry exhausted)\n"
        + _summary(processed=700, uploaded=0)
    )

    # Main batches set RUN_TYPE=retry in the workflow (all scheduled runs use
    # --retry-mode), so we test the 700/0 outage with run_type=retry — the
    # MIN_PROCESSED=50 gate must still let it trip the silent-ingestion alarm.
    result = _run_validator(tmp_path, log, run_type="retry")

    assert result.returncode == 1
    assert "silent-ingestion alarm" in result.stdout
    assert "0 uploads from 700 processed" in result.stdout


def test_silent_outage_at_threshold_boundary(tmp_path: Path):
    """PROCESSED=50 exactly trips the gate; PROCESSED=49 does not."""
    log_50 = _summary(processed=50, uploaded=0)
    log_49 = _summary(processed=49, uploaded=0)

    at_threshold = _run_validator(tmp_path / "at", log_50, run_type="retry")
    below_threshold = _run_validator(tmp_path / "below", log_49, run_type="retry")

    assert at_threshold.returncode == 1, "PROCESSED=50 must trip in retry mode"
    assert "silent-ingestion alarm" in at_threshold.stdout

    assert below_threshold.returncode == 0, "PROCESSED=49 must be treated as no-op"
    assert "skipping upload-percent check" in below_threshold.stdout


def test_silent_outage_promotions_mode_low_processed_still_fails(tmp_path: Path):
    """Promotions/manual runs keep the original PROCESSED > 0 trigger — a 3/0
    promotions run is a real outage, not a no-op."""
    log = _summary(processed=3, uploaded=0)

    result = _run_validator(tmp_path, log, run_type="promotions")

    assert result.returncode == 1
    assert "silent-ingestion alarm" in result.stdout


# ---------------------------------------------------------------------------
# JWT-thrash branch — the post-MASA-106 401-retry helper running hot is the
# MASA-107 signal. > 50 401-retries in one run = hard fail.
# ---------------------------------------------------------------------------
def test_jwt_thrash_fails(tmp_path: Path):
    body = [f"{LINE_PREFIX} 🔑 Authentication successful\n"]
    body += [
        f"{LINE_PREFIX} ⚠️ Got 401 on POST /api/prices/upload, token expired mid-run — re-auth\n"
        for _ in range(60)
    ]
    body += [_summary(processed=700, uploaded=650)]

    result = _run_validator(tmp_path, "".join(body), run_type="retry")

    assert result.returncode == 1
    assert "401-retry events" in result.stdout


def test_jwt_thrash_below_threshold_passes(tmp_path: Path):
    """50 re-auths exactly is fine; > 50 trips the gate."""
    body = [f"{LINE_PREFIX} 🔑 Authentication successful\n"]
    body += [
        f"{LINE_PREFIX} ⚠️ Got 401 on POST /api/prices/upload, token expired mid-run — re-auth\n"
        for _ in range(50)
    ]
    body += [_summary(processed=700, uploaded=650)]

    result = _run_validator(tmp_path, "".join(body), run_type="retry")

    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Missing log → exit 1 (scraper crashed before logging anything).
# ---------------------------------------------------------------------------
def test_missing_log_fails(tmp_path: Path):
    result = subprocess.run(
        ["bash", str(VALIDATOR), str(tmp_path / "does_not_exist.log"), "retry", "Lidl"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "not found" in result.stdout

#!/usr/bin/env bash
# Loud-fail health check for the daily-scraping workflow (MASA-108, tuned in
# MASA-147, regression-tested in MASA-159).
#
# Detects silent ingestion failures like the MASA-106 4h outage where every
# API call returned 401 but the workflow reported success. Hard-fails the
# caller if:
#   - log file missing (scraper crashed pre-logging)
#   - "❌ Authentication failed/error" > 0 (initial auth fatal)
#   - 401-retry-thrashing > 50 (post-MASA-106 helper running hot ⇒ JWT TTL too short)
#   - PROCESSED >= MIN_PROCESSED and UPLOADS < 5% of PROCESSED (silent failure)
#
# MIN_PROCESSED gate (MASA-147): retry-mode 6AM/8AM batches typically process
# only a handful of stragglers — 0 uploads from 3 processed is not a
# silent-ingestion outage. We require PROCESSED >= 50 in retry mode before
# applying the upload-percent check. Main batches (4AM) process ~700 products
# so the threshold trips immediately on a real outage. Promotions and manual
# runs keep the original PROCESSED > 0 trigger.
#
# Usage:
#   validate_scrape_log.sh <log_path> <run_type> <store_name>
#
# Exit codes:
#   0 — healthy
#   1 — silent-ingestion alarm, auth failure, or missing log
#
# The script reads no env vars by design so it is trivially testable from
# pytest fixtures with no setup.

set -u

LOG="${1:-}"
RUN_TYPE="${2:-retry}"
STORE_NAME="${3:-unknown}"

if [ -z "$LOG" ]; then
  echo "::error::validate_scrape_log.sh: missing required <log_path> arg" >&2
  exit 2
fi

if [ ! -f "$LOG" ]; then
  echo "::error::Log file $LOG not found — scraper crashed before producing logs."
  exit 1
fi

AUTH_FAILS=$(grep -cE "❌ Authentication (failed|error)" "$LOG" || true)
AUTH_RETRIES=$(grep -cE "Got 401 on .* token expired mid-run" "$LOG" || true)
# No leading anchor — Python logger prefixes lines with `timestamp - INFO -`,
# but "Processed:" / "Uploaded:" only appears in the end-of-run summary block.
PROCESSED=$(grep -E "Processed:\s+[0-9]+" "$LOG" | grep -oE "Processed:\s+[0-9]+" | grep -oE "[0-9]+" | tail -1)
UPLOADS=$(grep -E "Uploaded:\s+[0-9]+" "$LOG" | grep -oE "Uploaded:\s+[0-9]+" | grep -oE "[0-9]+" | tail -1)
PROCESSED=${PROCESSED:-0}
UPLOADS=${UPLOADS:-0}

if [ "$RUN_TYPE" = "retry" ]; then
  MIN_PROCESSED=50
else
  MIN_PROCESSED=1
fi

echo "::notice::${STORE_NAME} health: run_type=$RUN_TYPE processed=$PROCESSED uploads=$UPLOADS auth_fails=$AUTH_FAILS auth_retries=$AUTH_RETRIES min_processed=$MIN_PROCESSED"

FAIL=0
if [ "$AUTH_FAILS" -gt 0 ]; then
  echo "::error::${STORE_NAME}: $AUTH_FAILS fatal auth failures detected (initial login or unrecoverable 401)."
  FAIL=1
fi
if [ "$AUTH_RETRIES" -gt 50 ]; then
  echo "::error::${STORE_NAME}: $AUTH_RETRIES 401-retry events — JWT TTL likely too short, MASA-107 territory."
  FAIL=1
fi
if [ "$PROCESSED" -ge "$MIN_PROCESSED" ]; then
  PCT=$(( UPLOADS * 100 / PROCESSED ))
  if [ "$UPLOADS" -lt 5 ] || [ "$PCT" -lt 5 ]; then
    echo "::error::${STORE_NAME}: silent-ingestion alarm — $UPLOADS uploads from $PROCESSED processed (${PCT}%, threshold 5%, run_type=$RUN_TYPE)."
    FAIL=1
  fi
else
  echo "::notice::${STORE_NAME}: skipping upload-percent check — $PROCESSED processed below MIN_PROCESSED=$MIN_PROCESSED for run_type=$RUN_TYPE (retry-mode no-op is valid)."
fi
if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
echo "✅ ${STORE_NAME} health check passed: $UPLOADS/$PROCESSED uploaded (run_type=$RUN_TYPE), $AUTH_FAILS fatal auth fails, $AUTH_RETRIES re-auth events."

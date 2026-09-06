#!/usr/bin/env bash
# Decide the daily-scraping run mode from the cron expression that fired,
# not from the wall clock.
#
# GitHub delayed the Mon/Thu 04/06/08 UTC crons by 4–11 hours in late Aug
# 2026, so `date +%H` / `date +%u` checks picked the wrong mode. The event
# payload still carries the exact cron string in `github.event.schedule`,
# which is what this script keys on.
#
# Usage:
#   resolve_run_mode.sh <event_name> <schedule> <manual_promotions>
#     event_name         github.event_name  ("schedule" | "workflow_dispatch" | ...)
#     schedule           github.event.schedule (cron string; empty when not scheduled)
#     manual_promotions  workflow_dispatch input promotions_mode ("true" | "false" | "")
#
# Prints exactly one word: "promotions" or "retry".
# Exit 0 on success, 2 on usage error. Reads no env vars, so it is testable.
#
# Rules (first match wins):
#   1. manual_promotions == "true"        → promotions
#   2. schedule's day-of-week field == 0  → promotions (the Sunday scan)
#   3. anything else                      → retry

set -u

EVENT_NAME="${1:-}"
SCHEDULE="${2:-}"
MANUAL_PROMOTIONS="${3:-}"

if [ -z "$EVENT_NAME" ]; then
  echo "usage: resolve_run_mode.sh <event_name> <schedule> <manual_promotions>" >&2
  exit 2
fi

if [ "$MANUAL_PROMOTIONS" = "true" ]; then
  echo "promotions"
  exit 0
fi

if [ "$EVENT_NAME" = "schedule" ] && [ -n "$SCHEDULE" ]; then
  # 5th cron field = day of week. Only the Sunday cron (0) is the promotions scan.
  DOW=$(echo "$SCHEDULE" | awk '{print $5}')
  if [ "$DOW" = "0" ] || [ "$DOW" = "7" ]; then
    echo "promotions"
    exit 0
  fi
fi

echo "retry"

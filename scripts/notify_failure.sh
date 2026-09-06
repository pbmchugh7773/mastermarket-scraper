#!/usr/bin/env bash
# Turn a failed scraper run into a GitHub issue so somebody actually hears
# about it. Before 2026-09 the "Notify on Failure" steps only echoed text
# into the run log, and two Tesco outages went unnoticed for weeks.
#
# One open issue per workflow (label scraper-alert, title
# "[scraper-alert] <workflow>"): a repeat failure comments on the existing
# issue instead of opening a new one. Close the issue when the cause is fixed;
# the next failure opens a fresh one.
#
# Usage:
#   notify_failure.sh <workflow_name> <run_url> [details]
# Env:
#   GH_TOKEN  — github.token with `issues: write` on the calling job
#   GH_REPO   — owner/repo (set automatically inside Actions via github.repository)
#
# Exit 0 on success, 2 on usage error; a failing `gh` call fails the step
# loudly (the job is already red — a silent notifier is the bug we are fixing).

set -euo pipefail

WORKFLOW="${1:-}"
RUN_URL="${2:-}"
DETAILS="${3:-}"

if [ -z "$WORKFLOW" ] || [ -z "$RUN_URL" ]; then
  echo "usage: notify_failure.sh <workflow_name> <run_url> [details]" >&2
  exit 2
fi

LABEL="scraper-alert"
TITLE="[scraper-alert] ${WORKFLOW}"
WHEN="$(date -u '+%Y-%m-%d %H:%M UTC')"
BODY="**Run:** ${RUN_URL}
**When:** ${WHEN}

${DETAILS:-See the run log for the failing step.}

_Opened automatically by scripts/notify_failure.sh. Close this issue once the cause is fixed; the next failure opens a new one._"

# Idempotent label (--force updates colour/description if it already exists).
gh label create "$LABEL" --color B60205 --description "Scheduled scraper run failed" --force >/dev/null 2>&1 || true

EXISTING="$(gh issue list --state open --label "$LABEL" --search "\"${TITLE}\" in:title" \
  --json number,title --jq ".[] | select(.title == \"${TITLE}\") | .number" | head -1 || true)"

if [ -n "$EXISTING" ]; then
  gh issue comment "$EXISTING" --body "$BODY"
  echo "Commented on existing alert issue #${EXISTING}"
else
  URL="$(gh issue create --title "$TITLE" --label "$LABEL" --body "$BODY")"
  echo "Opened alert issue: ${URL}"
fi

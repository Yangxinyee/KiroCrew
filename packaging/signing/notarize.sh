#!/usr/bin/env bash
# notarize.sh -- submit a file to the Apple notary service and wait for the
# verdict, tolerating transient network errors.
#
# Why not `notarytool submit --wait`? `--wait` polls Apple from inside a
# single notarytool process, and ONE failed poll request (e.g.
# NSURLErrorDomain Code=-1001 "The request timed out." against
# appstoreconnect.apple.com/notary/v2/submissions/<id>) makes notarytool
# exit non-zero -- even when Apple has already Accepted the submission.
# Under `set -e` that kills the job and the whole publish chain behind it.
# This script owns the polling loop instead, so a transient request failure
# is retried with backoff inside the same overall time budget.
#
# Flow:  submit (json, no --wait) -> poll `notarytool info` -> verdict.
#   Accepted          -> exit 0
#   Invalid/Rejected  -> fetch the itemized `notarytool log`, exit 1 (fail-closed)
#   any call failure  -> treated as transient, retried with exponential backoff
#   budget exhausted  -> exit 1
#
# Retrying `submit` itself is safe: Apple issues notarization tickets per
# code-signature hash (cdhash) of the submitted content, so a duplicate
# submission of unchanged bytes re-attests exactly the same hashes -- it
# cannot yield a different verdict or a divergent ticket, and nothing is
# published by the submission alone. The only side effect is an extra row
# in `notarytool history`, and because Apple asks for at most 75
# notarizations per day the number of submit attempts is bounded
# (NOTARIZE_MAX_SUBMIT_ATTEMPTS, default 3) independently of the time budget.
#
# Usage: notarize.sh <file>
#
# Env (required): APPLE_ID, APPLE_PW (app-specific password), TEAM_ID
# Env (tuning, all in seconds):
#   NOTARIZE_BUDGET_SECS          total wall-clock budget (default 1800 = 30m,
#                                 same as the previous `--wait --timeout 30m`)
#   NOTARIZE_POLL_SECS            interval between healthy "In Progress" polls
#                                 (default 30)
#   NOTARIZE_RETRY_MIN_SECS       first backoff after a failed call (default 10)
#   NOTARIZE_RETRY_MAX_SECS       backoff ceiling (default 120)
#   NOTARIZE_MAX_SUBMIT_ATTEMPTS  submit attempts before giving up (default 3)
#
# Requires: xcrun notarytool (Xcode 13+), python3 (JSON parsing).

set -euo pipefail

FILE="${1:-}"
if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "Usage: $0 <file>" >&2
  exit 2
fi
for v in APPLE_ID APPLE_PW TEAM_ID; do
  if [ -z "${!v:-}" ]; then
    echo "ERROR: $v must be set in the environment" >&2
    exit 2
  fi
done

BUDGET_SECS="${NOTARIZE_BUDGET_SECS:-1800}"
POLL_SECS="${NOTARIZE_POLL_SECS:-30}"
RETRY_MIN_SECS="${NOTARIZE_RETRY_MIN_SECS:-10}"
RETRY_MAX_SECS="${NOTARIZE_RETRY_MAX_SECS:-120}"
MAX_SUBMIT_ATTEMPTS="${NOTARIZE_MAX_SUBMIT_ATTEMPTS:-3}"

START_TS=$(date +%s)
ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT

elapsed() { echo $(( $(date +%s) - START_TS )); }

log() { printf '[notarize %4ds] %s\n' "$(elapsed)" "$*"; }

# Sleep for $1 seconds unless that would overrun the budget, in which case
# fail. Every wait in this script goes through here, so the budget is a hard
# ceiling no matter how many transient errors are absorbed.
sleep_within_budget() {
  local secs="$1"
  if [ $(( $(elapsed) + secs )) -gt "$BUDGET_SECS" ]; then
    echo "ERROR: notarization budget of ${BUDGET_SECS}s exhausted" \
      "(file: $FILE, submission: ${SUBMISSION_ID:-<none>})" >&2
    exit 1
  fi
  sleep "$secs"
}

# Run notarytool with the credentials appended. stdout -> $OUT, stderr ->
# $ERR_FILE, exit code -> $RC (never aborts the script under set -e).
notary() {
  RC=0
  OUT=$(xcrun notarytool "$@" \
    --apple-id "$APPLE_ID" --password "$APPLE_PW" --team-id "$TEAM_ID" \
    --output-format json 2>"$ERR_FILE") || RC=$?
}

# Extract a top-level string field from notarytool's JSON stdout. Prints
# nothing (and returns non-zero) when the output is not JSON or lacks the
# field, which the callers treat as a transient failure.
json_field() {
  printf '%s' "$OUT" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get(sys.argv[1], "")
except Exception:
    v = ""
print(v)
sys.exit(0 if v else 1)' "$1"
}

# Log the failed call (what we know from the tool) and back off. The
# backoff doubles up to RETRY_MAX_SECS and resets whenever a call succeeds.
# Any failure counts as transient: the classic signatures are
# NSURLErrorDomain / "The request timed out." / HTTP 5xx, but a network
# blip can surface in other shapes too, and the time budget bounds the
# worst case either way.
RETRY_SECS="$RETRY_MIN_SECS"
backoff_after_failure() {
  local what="$1"
  log "$what failed (exit $RC); treating as transient. Tool output:"
  [ -n "$OUT" ] && printf '%s\n' "$OUT"
  sed 's/^/  stderr: /' "$ERR_FILE" || true
  log "retrying in ${RETRY_SECS}s"
  sleep_within_budget "$RETRY_SECS"
  RETRY_SECS=$(( RETRY_SECS * 2 ))
  [ "$RETRY_SECS" -gt "$RETRY_MAX_SECS" ] && RETRY_SECS="$RETRY_MAX_SECS"
  return 0
}

# ── 1. Submit ───────────────────────────────────────────────────────────────
SUBMISSION_ID=""
attempt=0
while [ -z "$SUBMISSION_ID" ]; do
  attempt=$(( attempt + 1 ))
  if [ "$attempt" -gt "$MAX_SUBMIT_ATTEMPTS" ]; then
    echo "ERROR: notarytool submit failed ${MAX_SUBMIT_ATTEMPTS} times for $FILE" >&2
    exit 1
  fi
  log "submitting $FILE to Apple notary service (team $TEAM_ID, attempt $attempt/$MAX_SUBMIT_ATTEMPTS)"
  notary submit "$FILE"
  if [ "$RC" -eq 0 ] && SUBMISSION_ID=$(json_field id); then
    log "submitted: id $SUBMISSION_ID"
    RETRY_SECS="$RETRY_MIN_SECS"
  else
    SUBMISSION_ID=""
    backoff_after_failure "submit"
  fi
done

# ── 2. Poll until a verdict ─────────────────────────────────────────────────
while :; do
  notary info "$SUBMISSION_ID"
  if [ "$RC" -ne 0 ] || ! STATUS=$(json_field status); then
    backoff_after_failure "info $SUBMISSION_ID"
    continue
  fi
  RETRY_SECS="$RETRY_MIN_SECS"

  case "$STATUS" in
    Accepted)
      log "notarization Accepted: $SUBMISSION_ID"
      exit 0
      ;;
    Invalid|Rejected)
      # Fail-closed: a real verdict from Apple. Pull the itemized log so
      # the job output says WHY (this is the only place it is recorded).
      echo "ERROR: notarization $STATUS for $FILE (submission $SUBMISSION_ID)." \
        "Fetching the itemized Apple log..." >&2
      notary log "$SUBMISSION_ID" || true
      printf '%s\n' "$OUT"
      cat "$ERR_FILE" >&2 || true
      exit 1
      ;;
    *)
      # "In Progress" (or any status we do not recognise -- keep waiting,
      # the budget bounds it).
      log "status: $STATUS"
      sleep_within_budget "$POLL_SECS"
      ;;
  esac
done

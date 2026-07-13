#!/usr/bin/env bash
#
# Review-agent liveness sensor (story #105).
#
# Decides whether the Claude review agent actually POSTED a substantive review
# comment for the current run, or passed SILENTLY (the known failure mode: job
# goes green in <1min having posted nothing). Pure detection — never fails the
# build itself; the caller decides what to do with the verdict.
#
# A comment counts as "this run's output" when it is authored by the review bot
# AND created after this run started. Keying on run-start time (not head sha) is
# the deliberately-simple approach: it can't mistake an OLD comment from a
# previous sha for liveness on this one, which is exactly the trap to avoid.
#
# We look in two places, because the agent posts a mandatory summary as a PR
# *issue* comment and its findings as inline *review* comments — either one
# proves the agent produced output, so a partial review (inline-only) is not
# treated as silent (and therefore won't trigger a duplicate-posting re-run).
#
# Required env:
#   REPO            owner/name (github.repository)
#   PR              pull request number
#   SINCE           ISO-8601 UTC instant; comments at/before this are ignored
# Optional env:
#   BOT_LOGIN       review bot login (default: claude[bot])
#   FIXTURE_DIR     if set, read <dir>/issue.json and <dir>/review.json instead
#                   of calling `gh api` (offline testing)
#   GITHUB_OUTPUT   if set, appends `posted=true|false`
#
# Exit status is always 0 (detection only). Prints one diagnostic line.

set -euo pipefail

: "${REPO:?REPO is required}"
: "${PR:?PR is required}"
: "${SINCE:?SINCE is required}"
BOT_LOGIN="${BOT_LOGIN:-claude[bot]}"

# Fetch a comments collection as a JSON array, from a fixture file when offline.
fetch() {
  local label="$1" api_path="$2"
  if [ -n "${FIXTURE_DIR:-}" ]; then
    cat "${FIXTURE_DIR}/${label}.json"
  else
    # per_page=100 is plenty for a single PR's review comments; on any API error
    # fall back to an empty array so the sensor degrades to "silent" rather than
    # crashing the workflow.
    gh api "repos/${REPO}/${api_path}?per_page=100" 2>/dev/null || echo '[]'
  fi
}

# Count array elements authored by $BOT_LOGIN and created strictly after $SINCE.
# ISO-8601 UTC (…Z) timestamps compare correctly as strings.
count_recent() {
  jq --arg bot "$BOT_LOGIN" --arg since "$SINCE" \
    '[ .[] | select((.user.login == $bot) and (.created_at > $since)) ] | length'
}

issue_json="$(fetch issue "issues/${PR}/comments")"
review_json="$(fetch review "pulls/${PR}/comments")"

issue_count="$(printf '%s' "$issue_json" | count_recent)"
review_count="$(printf '%s' "$review_json" | count_recent)"
total=$((issue_count + review_count))

if [ "$total" -gt 0 ]; then
  status="posted"
  posted="true"
else
  status="silent"
  posted="false"
fi

echo "[review-liveness] since=${SINCE} bot=${BOT_LOGIN} issue_comments=${issue_count} review_comments=${review_count} -> status=${status}"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "posted=${posted}" >>"$GITHUB_OUTPUT"
fi

#!/usr/bin/env bash
#
# Offline unit test for check-review-liveness.sh (story #105).
#
# Drives the sensor with canned `gh api` JSON via FIXTURE_DIR (no network, no
# Actions runtime) and asserts the posted/silent verdict for each scenario. Run:
#   .github/scripts/test-check-review-liveness.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="${HERE}/check-review-liveness.sh"
SINCE="2026-07-13T12:00:00Z"
BOT="claude[bot]"

fails=0

# Run the sensor against a fixture dir, capture the emitted `posted=` output.
run_case() {
  local name="$1" fixture_dir="$2" want="$3"
  local out_file
  out_file="$(mktemp)"
  GITHUB_OUTPUT="$out_file" REPO="acme/nebula" PR="42" SINCE="$SINCE" BOT_LOGIN="$BOT" \
    FIXTURE_DIR="$fixture_dir" bash "$SUT" >/dev/null
  local got
  got="$(grep '^posted=' "$out_file" | tail -n1 | cut -d= -f2)"
  rm -f "$out_file"
  if [ "$got" = "$want" ]; then
    echo "ok   - ${name} (posted=${got})"
  else
    echo "FAIL - ${name}: want posted=${want}, got posted=${got}"
    fails=$((fails + 1))
  fi
}

mk() { # mk <dir> <issue.json contents> <review.json contents>
  local d="$1"
  mkdir -p "$d"
  printf '%s' "$2" >"$d/issue.json"
  printf '%s' "$3" >"$d/review.json"
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# 1. Fresh summary comment after run start -> posted.
mk "$tmp/summary" \
  '[{"user":{"login":"claude[bot]"},"created_at":"2026-07-13T12:03:00Z","body":"Reviewed 4 files, no findings."}]' \
  '[]'
run_case "summary comment after start is live" "$tmp/summary" "true"

# 2. Only an OLD bot comment (before run start) -> silent (the key trap).
mk "$tmp/stale" \
  '[{"user":{"login":"claude[bot]"},"created_at":"2026-07-13T11:59:00Z","body":"old review for a previous sha"}]' \
  '[]'
run_case "stale comment does not count as liveness" "$tmp/stale" "false"

# 3. No comments at all -> silent (the failure mode we detect).
mk "$tmp/empty" '[]' '[]'
run_case "no comments is silent" "$tmp/empty" "false"

# 4. Inline review comment only (no summary) -> posted (partial review, not silent).
mk "$tmp/inline" \
  '[]' \
  '[{"user":{"login":"claude[bot]"},"created_at":"2026-07-13T12:04:30Z","body":"nit: rename this"}]'
run_case "inline-only review still counts as live" "$tmp/inline" "true"

# 5. A fresh comment from a DIFFERENT author (e.g. a human) -> silent.
mk "$tmp/human" \
  '[{"user":{"login":"some-human"},"created_at":"2026-07-13T12:05:00Z","body":"lgtm"}]' \
  '[]'
run_case "non-bot comment is not liveness" "$tmp/human" "false"

# 6. Comment exactly at SINCE (boundary, not strictly after) -> silent.
mk "$tmp/boundary" \
  '[{"user":{"login":"claude[bot]"},"created_at":"2026-07-13T12:00:00Z","body":"exactly at start"}]' \
  '[]'
run_case "comment exactly at start is excluded" "$tmp/boundary" "false"

echo
if [ "$fails" -eq 0 ]; then
  echo "All review-liveness sensor cases passed."
else
  echo "${fails} case(s) FAILED."
  exit 1
fi

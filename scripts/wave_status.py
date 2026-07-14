#!/usr/bin/env python3
"""Snapshot the current parallel wave into one JSON artifact.

A "wave" is a set of roadmap stories built in parallel, one per `feat/<n>-<slug>`
branch (see the `wave` skill + CLAUDE.md "Parallel story work"). This script is
pure tooling: stdlib only, no backend deps, no new services. It shells out to
`git` and `gh` (both already required for wave work) and writes a single JSON
file that the companion `wave_status.html` view polls.

Run it once (`make wave-status`) or on a loop (`make wave-watch`); the view is a
static page served from this directory (`python3 -m http.server`).

--------------------------------------------------------------------------------
JSON SCHEMA (v1) — STABLE. Agents consume this; add fields, don't repurpose.
--------------------------------------------------------------------------------
{
  "schema_version": 1,
  "generated_at":  "2026-07-13T12:00:00Z",   # UTC ISO-8601
  "repo":          "owner/name" | null,        # from `gh repo view`, best-effort
  "gh_available":  true,                        # false => PR/check/review null
  "since_hours":   24,                          # merged-PR inclusion window
  "stories": [
    {
      "branch":   "feat/107-wave-sidecar",
      "story":    107 | null,                   # parsed from branch, null if none
      "slug":     "wave-sidecar",
      "worktree": "/abs/path" | null,           # local worktree checkout, if any
      "pr": null | {
        "number":    108,
        "url":       "https://github.com/owner/name/pull/108",
        "state":     "OPEN" | "MERGED" | "CLOSED",
        "merged":    false,
        "mergeable": "MERGEABLE" | "CONFLICTING" | "UNKNOWN",
        "merged_at": "2026-...Z" | null
      },
      "checks": [                               # [] if none registered / no PR
        {"name": "backend — lint + test", "state": "SUCCESS", "bucket": "pass"}
        # bucket in: pass | fail | pending | skipping | cancel
      ],
      "checks_state": "pass"|"fail"|"pending"|"none",  # rollup over buckets
      "review": null | {
        "verdict":    "approve"|"needs-changes"|"placeholder"|"unclear",
        "text":       "<last paragraph of the review comment, verbatim>",
        "created_at": "2026-...Z"
      },
      "anomalies": {
        # states that are otherwise INVISIBLE — surfaced distinctly in the view
        "review_silent":  false,  # review check passed but no review comment
        "conflicted":     false,  # PR mergeable=CONFLICTING => checks never run
        "checks_pending": false   # checks in-flight / not yet registered
      },
      "impact": {                               # ADDITIVE (#124) — code-health footprint
        "files":         ["backend/app/graph/x.py", ...],  # changed vs origin/main
        "layers":        ["graph", "tools"],    # distinct CODE layers, low->high
        "n_code_layers": 2,
        "guarded":       ["backend/app/auth.py"],  # guarded-path touches (may be [])
        "risk":          "low"|"medium"|"high",    # code_health.story_risk()
        "deltas": null | {                      # metrics vs main; null if not computable
          "source_loc": 12, "test_loc": 40, "test_count": 3,
          "upward_imports": 0, "cross_layer_edges": 0,
          "complex_functions": 0, "long_functions": 1,
          "noqa_count": 0, "test_ratio": 0.004
        }
      }
    }
  ]
}
Note: `impact` is an additive v1 extension — schema_version stays 1. Consumers
that predate it must tolerate its absence; here it is always present, but
`files`/`deltas` degrade to []/null when git or the branch worktree is missing.
--------------------------------------------------------------------------------

Wave membership (branch-pattern driven, no manual registry):
  - every `feat/*` branch checked out in a local git worktree, PLUS
  - every `feat/*` branch with an OPEN pull request, PLUS
  - every `feat/*` branch whose PR merged within --since-hours (default 24),
    so the just-merged tail of the wave stays visible.
Stale long-merged remote branches drop out on their own.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# code_health.py sits next to this script; make it importable regardless of cwd
# so the per-branch impact block (#124) can reuse its layer map + risk heuristic.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_health  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_SINCE_HOURS = 24
GH_FALLBACK = "/opt/homebrew/bin/gh"

BRANCH_RE = re.compile(r"^feat/(\d+)-(.+)$")

# Guarded paths (#124): security- and write-path files where ANY change lifts a
# story's risk to HIGH regardless of how few layers it spans — auth verification,
# the durable-job dispatch table, the graph write repository, and the
# propose->commit paths that are the only sanctioned route from an agent
# proposal to a graph write (human-in-the-loop). Matched exactly (repo-relative).
GUARDED_PATHS = frozenset({
    "backend/app/auth.py",
    "backend/app/mcp_server.py",
    "backend/app/graph/jobs.py",
    "backend/app/graph/repository.py",
    "backend/app/agents/assistant/proposals.py",
    "backend/app/agents/people/proposals.py",
    "backend/app/agents/deals/proposals.py",
})


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested via --selftest — no git/gh/network needed)
# ----------------------------------------------------------------------------

def parse_branch(branch: str) -> tuple[int | None, str]:
    """feat/107-wave-sidecar -> (107, "wave-sidecar").

    Non-conforming `feat/*` branches (no leading story number) get story=None
    and the remainder after `feat/` as the slug.
    """
    m = BRANCH_RE.match(branch)
    if m:
        return int(m.group(1)), m.group(2)
    slug = branch[len("feat/"):] if branch.startswith("feat/") else branch
    return None, slug


def last_paragraph(body: str) -> str:
    """Return the last non-empty paragraph of a markdown comment body."""
    if not body:
        return ""
    paras = re.split(r"\n\s*\n", body.replace("\r\n", "\n").strip())
    for para in reversed(paras):
        p = para.strip()
        if p:
            return p
    return ""


def classify_verdict(text: str) -> str:
    """Map a review comment's last paragraph to a stable verdict label.

    Verdicts live in the last paragraph by convention (usually "**Overall: …**").
    """
    t = text.lower()
    # order matters: a "needs changes" must win over an incidental "looks"
    if re.search(r"needs?\s+changes|request(?:s|ing)?\s+changes|"
                 r"requires?\s+changes|not\s+ready|blocking", t):
        return "needs-changes"
    if "placeholder" in t:
        return "placeholder"
    if re.search(r"looks?\s+good|lgtm|approve|ship\s+it|good\s+to\s+(?:go|merge)|"
                 r"no\s+(?:blocking\s+)?(?:issues|concerns)", t):
        return "approve"
    return "unclear"


def latest_review_comment(comments: list[dict]) -> dict | None:
    """Pick the most recent claude[bot] review comment from a PR's comments.

    `gh pr view --json comments` items look like
    {"author": {"login": "claude"}, "body": "...", "createdAt": "..."}.
    Bot login shows as "claude" or "claude[bot]" depending on the surface.
    """
    bot = [
        c for c in comments
        if re.search(r"claude", (c.get("author") or {}).get("login", ""), re.I)
    ]
    if not bot:
        return None
    return max(bot, key=lambda c: c.get("createdAt", ""))


def summarize_checks(checks: list[dict]) -> str:
    """Roll per-check buckets up to one state: fail > pending > none > pass."""
    if not checks:
        return "none"
    buckets = {c.get("bucket") for c in checks}
    if buckets & {"fail", "cancel"}:
        return "fail"
    if "pending" in buckets:
        return "pending"
    # all pass / skipping
    if buckets <= {"pass", "skipping"}:
        return "pass"
    return "pending"


def derive_anomalies(
    pr: dict | None,
    checks: list[dict],
    review: dict | None,
    review_check_passed: bool,
) -> dict:
    """Detect the invisible states that matter most in a wave.

    - conflicted: a CONFLICTING PR builds no test-merge commit, so its checks
      never register — looks identical to an Actions outage.
    - review_silent: the `review` check went green but the bot posted no
      verdict comment (the silent-shrug) — the diff got no real review.
    - checks_pending: checks in-flight, or not yet registered on the head sha
      (an empty array on an open, non-conflicted PR).
    """
    conflicted = bool(pr) and pr.get("mergeable") == "CONFLICTING"
    is_open = bool(pr) and pr.get("state") == "OPEN" and not pr.get("merged")

    review_silent = bool(is_open and review_check_passed and review is None)

    checks_state = summarize_checks(checks)
    checks_pending = bool(
        is_open and not conflicted
        and (checks_state == "pending" or not checks)
    )
    return {
        "review_silent": review_silent,
        "conflicted": conflicted,
        "checks_pending": checks_pending,
    }


# ----------------------------------------------------------------------------
# git / gh plumbing (impure — skipped by --selftest)
# ----------------------------------------------------------------------------

def find_gh() -> str | None:
    """Locate the gh binary. `command -v gh` may resolve to a shell alias
    (`op plugin run -- gh`) in this repo's env, so prefer the real binary and
    fall back to the known Homebrew path."""
    path = shutil.which("gh")
    if path and Path(path).is_file():
        return path
    if Path(GH_FALLBACK).is_file():
        return GH_FALLBACK
    return None


def _run(cmd: list[str], timeout: int = 30) -> str | None:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _run_json(cmd: list[str], timeout: int = 30):
    raw = _run(cmd, timeout=timeout)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def worktree_branches() -> dict[str, str]:
    """Map each feat/* branch checked out in a worktree to its abs path."""
    raw = _run(["git", "worktree", "list", "--porcelain"]) or ""
    out: dict[str, str] = {}
    path = None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            name = ref.removeprefix("refs/heads/")
            if name.startswith("feat/") and path:
                out[name] = path
    return out


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_pr_index(gh: str, since_hours: int) -> dict[str, dict]:
    """Branch -> PR record, for OPEN feat/* PRs plus feat/* PRs merged recently."""
    idx: dict[str, dict] = {}
    fields = "number,url,state,mergeable,mergedAt,headRefName"
    open_prs = _run_json(
        [gh, "pr", "list", "--state", "open", "--limit", "100", "--json", fields]
    ) or []
    for pr in open_prs:
        head = pr.get("headRefName", "")
        if head.startswith("feat/"):
            idx[head] = _pr_record(pr)

    merged = _run_json(
        [gh, "pr", "list", "--state", "merged", "--limit", "50", "--json", fields]
    ) or []
    cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    for pr in merged:
        head = pr.get("headRefName", "")
        if not head.startswith("feat/") or head in idx:
            continue
        merged_at = parse_iso(pr.get("mergedAt"))
        if merged_at and merged_at.timestamp() >= cutoff:
            idx[head] = _pr_record(pr)
    return idx


def _pr_record(pr: dict) -> dict:
    state = pr.get("state", "UNKNOWN")
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "state": state,
        "merged": state == "MERGED",
        "mergeable": pr.get("mergeable", "UNKNOWN"),
        "merged_at": pr.get("mergedAt"),
    }


def collect_checks(gh: str, pr_number: int) -> list[dict]:
    data = _run_json(
        [gh, "pr", "checks", str(pr_number), "--json", "name,state,bucket"]
    )
    if not data:
        return []
    return [
        {"name": c.get("name"), "state": c.get("state"), "bucket": c.get("bucket")}
        for c in data
    ]


def collect_review(gh: str, pr_number: int) -> dict | None:
    data = _run_json([gh, "pr", "view", str(pr_number), "--json", "comments"])
    if not data:
        return None
    comment = latest_review_comment(data.get("comments", []))
    if not comment:
        return None
    text = last_paragraph(comment.get("body", ""))
    return {
        "verdict": classify_verdict(text),
        "text": text,
        "created_at": comment.get("createdAt"),
    }


def review_check_passed(checks: list[dict]) -> bool:
    for c in checks:
        if (c.get("name") or "").strip().lower() == "review":
            return c.get("bucket") == "pass"
    return False


# ----------------------------------------------------------------------------
# Per-branch code-health impact (#124) — layer footprint + guarded + risk
# ----------------------------------------------------------------------------

def impact_footprint(files: list[str]) -> dict:
    """Pure: changed-file list -> {files, layers, n_code_layers, guarded, risk}.

    `layers` are distinct backend CODE layers (foundation < graph < tools <
    capture/agents < surfaces), ordered low->high; tooling/frontend files map to
    no layer. `guarded` are exact guarded-path touches. Risk comes from the one
    shared heuristic in code_health.story_risk (unit-tested there).
    """
    layers: list[str] = []
    for f in files:
        lyr = code_health.layer_for_path(f)
        if lyr and lyr not in layers:
            layers.append(lyr)
    layers.sort(key=lambda name: code_health.LAYER_RANK.get(name, 99))
    guarded = sorted(f for f in files if f in GUARDED_PATHS)
    return {
        "files": files,
        "layers": layers,
        "n_code_layers": len(layers),
        "guarded": guarded,
        "risk": code_health.story_risk(len(layers), bool(guarded)),
    }


def diff_files(branch: str, worktree: str | None) -> list[str]:
    """Repo-relative files changed on a branch vs origin/main (three-dot diff).

    Prefers the local branch ref when a worktree exists, else the remote-tracking
    ref. Returns [] when git can't answer (origin/main not fetched, etc.)."""
    ref = branch if worktree else f"origin/{branch}"
    raw = _run(["git", "diff", "--name-only", f"origin/main...{ref}"])
    if raw is None:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def baseline_metrics() -> dict | None:
    """main's metrics from the code-health history cache (for delta baselines).

    Prefers the entry for origin/main's HEAD sha; falls back to the newest
    cached commit. None when --history has never been run."""
    cache = code_health.load_history_cache()
    if not cache:
        return None
    # Same ref resolution as code_health.build_history (origin/main preferred,
    # local main fallback) so the cache lookup and the history are keyed alike.
    sha = ""
    for candidate in ("origin/main", "main"):
        got = (_run(["git", "rev-parse", "--verify", "--quiet", candidate]) or "").strip()
        if got:
            sha = got
            break
    entry = cache.get(sha)
    if entry:
        return entry.get("metrics")
    newest = max(cache.values(), key=lambda e: e.get("ts", 0), default=None)
    return newest.get("metrics") if newest else None


def metric_deltas(worktree: str | None, baseline: dict | None) -> dict | None:
    """branch worktree metrics minus main baseline, per metric. None if either
    is unavailable (remote-only branch, or no history cache) — skipped, not zero."""
    if not worktree or baseline is None:
        return None
    try:
        cur = code_health.analyze_tree(worktree)
    except Exception:
        return None
    deltas: dict = {}
    for key in code_health.METRIC_KEYS:
        cur_v = cur.get(key, 0)
        base_v = baseline.get(key, 0)
        diff = cur_v - base_v
        deltas[key] = round(diff, 4) if isinstance(diff, float) else diff
    return deltas


def story_impact(branch: str, worktree: str | None, baseline: dict | None) -> dict:
    """Assemble one story's impact block; never raises (best-effort sidecar)."""
    try:
        files = diff_files(branch, worktree)
        impact = impact_footprint(files)
        impact["deltas"] = metric_deltas(worktree, baseline)
        return impact
    except Exception:
        return {"files": [], "layers": [], "n_code_layers": 0,
                "guarded": [], "risk": "low", "deltas": None}


# ----------------------------------------------------------------------------
# Snapshot assembly
# ----------------------------------------------------------------------------

def build_snapshot(since_hours: int) -> dict:
    gh = find_gh()
    worktrees = worktree_branches()
    baseline = baseline_metrics()

    pr_index: dict[str, dict] = {}
    repo = None
    if gh:
        repo_info = _run_json([gh, "repo", "view", "--json", "nameWithOwner"])
        if repo_info:
            repo = repo_info.get("nameWithOwner")
        pr_index = collect_pr_index(gh, since_hours)

    branches = sorted(set(worktrees) | set(pr_index))

    stories = []
    for branch in branches:
        story, slug = parse_branch(branch)
        pr = pr_index.get(branch)
        checks: list[dict] = []
        review: dict | None = None
        if gh and pr and pr.get("number"):
            checks = collect_checks(gh, pr["number"])
            review = collect_review(gh, pr["number"])
        anomalies = derive_anomalies(
            pr, checks, review, review_check_passed(checks)
        )
        stories.append({
            "branch": branch,
            "story": story,
            "slug": slug,
            "worktree": worktrees.get(branch),
            "pr": pr,
            "checks": checks,
            "checks_state": summarize_checks(checks),
            "review": review,
            "anomalies": anomalies,
            "impact": story_impact(branch, worktrees.get(branch), baseline),
        })

    # order: lowest story number first, unnumbered last
    stories.sort(key=lambda s: (s["story"] is None, s["story"] or 0, s["branch"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": repo,
        "gh_available": gh is not None,
        "since_hours": since_hours,
        "stories": stories,
    }


# ----------------------------------------------------------------------------
# Self-test (canned fixtures — the pure layer only)
# ----------------------------------------------------------------------------

def _selftest() -> int:
    failures: list[str] = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # branch -> story parsing
    check("parse feat", parse_branch("feat/107-wave-sidecar"),
          (107, "wave-sidecar"))
    check("parse multi-dash", parse_branch("feat/40-add-person-agent"),
          (40, "add-person-agent"))
    check("parse no-number", parse_branch("feat/capture-button"),
          (None, "capture-button"))
    check("parse non-feat", parse_branch("chore/foo"), (None, "chore/foo"))

    # verdict extraction from a comment body (last paragraph is the verdict)
    good_body = (
        "## Review — PR #99\n\nSome detailed prose about the diff.\n\n"
        "**Overall: looks good**, modulo adding the missing test."
    )
    bad_body = (
        "## Review\n\nProse.\n\n"
        "**Overall: needs changes** — one correctness gap before merge."
    )
    check("last para good", last_paragraph(good_body),
          "**Overall: looks good**, modulo adding the missing test.")
    check("verdict good", classify_verdict(last_paragraph(good_body)), "approve")
    check("verdict bad", classify_verdict(last_paragraph(bad_body)),
          "needs-changes")
    check("verdict placeholder", classify_verdict("placeholder"), "placeholder")
    check("verdict lgtm", classify_verdict("LGTM, ship it"), "approve")
    check("verdict empty", classify_verdict(""), "unclear")

    # latest review comment selection (case-insensitive login, newest wins)
    comments = [
        {"author": {"login": "yrnclndymn"}, "body": "human note",
         "createdAt": "2026-07-13T10:00:00Z"},
        {"author": {"login": "claude"}, "body": "old review",
         "createdAt": "2026-07-13T09:00:00Z"},
        {"author": {"login": "claude[bot]"}, "body": "new review",
         "createdAt": "2026-07-13T11:00:00Z"},
    ]
    check("latest review body",
          (latest_review_comment(comments) or {}).get("body"), "new review")
    check("no review", latest_review_comment(
        [{"author": {"login": "someone"}, "body": "x", "createdAt": "z"}]), None)

    # checks rollup
    passing = [{"bucket": "pass"}, {"bucket": "skipping"}]
    failing = [{"bucket": "pass"}, {"bucket": "fail"}]
    pending = [{"bucket": "pass"}, {"bucket": "pending"}]
    check("checks pass", summarize_checks(passing), "pass")
    check("checks fail", summarize_checks(failing), "fail")
    check("checks pending", summarize_checks(pending), "pending")
    check("checks none", summarize_checks([]), "none")

    # anomaly derivation
    open_pr = {"state": "OPEN", "merged": False, "mergeable": "MERGEABLE"}
    conflict_pr = {"state": "OPEN", "merged": False, "mergeable": "CONFLICTING"}
    merged_pr = {"state": "MERGED", "merged": True, "mergeable": "UNKNOWN"}

    # silent review: open PR, review check passed, no comment
    a = derive_anomalies(open_pr, [{"name": "review", "bucket": "pass"}],
                         None, True)
    check("review_silent true", a["review_silent"], True)
    check("review_silent not pending", a["checks_pending"], False)

    # review present -> not silent
    a = derive_anomalies(open_pr, [{"name": "review", "bucket": "pass"}],
                         {"verdict": "approve"}, True)
    check("review not silent", a["review_silent"], False)

    # conflicted PR: conflicted true, checks_pending suppressed even if empty
    a = derive_anomalies(conflict_pr, [], None, False)
    check("conflicted true", a["conflicted"], True)
    check("conflicted suppresses pending", a["checks_pending"], False)

    # open PR, empty checks (not yet registered) -> pending, not conflicted
    a = derive_anomalies(open_pr, [], None, False)
    check("empty checks pending", a["checks_pending"], True)
    check("empty checks not conflicted", a["conflicted"], False)

    # in-flight checks -> pending
    a = derive_anomalies(open_pr, pending, None, False)
    check("inflight pending", a["checks_pending"], True)

    # merged PR: no anomalies
    a = derive_anomalies(merged_pr, passing, {"verdict": "approve"}, True)
    check("merged no silent", a["review_silent"], False)
    check("merged no pending", a["checks_pending"], False)
    check("merged no conflict", a["conflicted"], False)

    # per-branch impact footprint (#124) — pure layer/guarded/risk projection
    tooling = impact_footprint(["scripts/x.py", "frontend/src/App.tsx"])
    check("impact tooling layers", tooling["layers"], [])
    check("impact tooling risk", tooling["risk"], "low")
    one = impact_footprint(["backend/app/graph/x.py", "backend/tests/test_x.py"])
    check("impact one layer", one["layers"], ["graph"])
    check("impact one risk", one["risk"], "low")
    two = impact_footprint(["backend/app/graph/x.py", "backend/app/tools/web.py"])
    check("impact two layers", two["layers"], ["graph", "tools"])   # low->high
    check("impact two risk", two["risk"], "medium")
    three = impact_footprint([
        "backend/app/graph/x.py", "backend/app/tools/web.py",
        "backend/app/api/routes.py",
    ])
    check("impact three risk", three["risk"], "high")
    guarded = impact_footprint(["backend/app/auth.py"])   # 1 layer but guarded
    check("impact guarded list", guarded["guarded"], ["backend/app/auth.py"])
    check("impact guarded risk", guarded["risk"], "high")

    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("selftest OK (all pure-function assertions passed)")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--selftest", action="store_true",
                        help="run pure-function unit checks and exit")
    parser.add_argument("--since-hours", type=int, default=DEFAULT_SINCE_HOURS,
                        help="include feat/* PRs merged within this window "
                             f"(default {DEFAULT_SINCE_HOURS})")
    parser.add_argument("-o", "--output",
                        help="write JSON here (default: wave-status.json next "
                             "to this script)")
    parser.add_argument("--stdout", action="store_true",
                        help="print JSON to stdout instead of writing a file")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    snapshot = build_snapshot(args.since_hours)
    payload = json.dumps(snapshot, indent=2)

    if args.stdout:
        print(payload)
        return 0

    out = Path(args.output) if args.output else Path(__file__).parent / "wave-status.json"
    out.write_text(payload + "\n")
    n = len(snapshot["stories"])
    print(f"wrote {out} — {n} stor{'y' if n == 1 else 'ies'}"
          f"{'' if snapshot['gh_available'] else ' (gh unavailable: PR data null)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

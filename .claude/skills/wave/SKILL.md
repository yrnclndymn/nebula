---
name: wave
description: >
  Run a wave of roadmap stories in parallel: pick a dependency-free set of
  GitHub issues, launch one story-worker agent per story in isolated worktrees,
  then integrate sequentially through CI + the review agent. Use when asked to
  "launch a wave" / build several stories in parallel.
---

# Wave: parallel story development for Nebula

The orchestrating session runs this loop; workers use the `story-worker` agent
type (`.claude/agents/story-worker.md`), which carries the worker contract.

## 1. Pick the wave

- Choose 2–4 stories from the "Nebula Roadmap" project that are **mutually
  independent**: no story depends on another's output, and their file
  footprints don't overlap beyond the shared hot files.
- Where two stories touch the same seam, either resequence (stages within the
  wave) or write an explicit ownership split into both briefs
  ("#A owns file X / the endpoint; #B must not touch it").
- Move the board cards to *In Progress*.

## 2. Launch

- One `Agent` call per story: `subagent_type: story-worker`,
  `isolation: worktree`, background (the default).
- The brief needs only: the issue number (+ inline body if helpful), the
  wave-specific SCOPE BOUNDARIES, and any design constraints the issue doesn't
  capture. The contract (conventions, DoD, TDD default, report format) is in
  the agent definition — don't repeat it.

## 3. Integrate (per finished worker, sequentially)

1. Review the diff yourself first — especially anything touching guards,
   auth, or irreversible graph operations.
2. Push explicitly: `git push origin <branch>` (a bare `git push` from a
   worktree without upstream **silently no-ops** — always verify the remote
   sha moved).
3. Open the PR with `Closes #<issue>`.
4. **Poll CI + review in a background Bash task** (`run_in_background: true`)
   and continue other work — never sit in a foreground sleep loop burning the
   main context. Act on the notification.
5. Triage the review: fix real findings in the worktree (commit + push
   re-triggers checks); rebut with a PR comment where the reviewer is wrong.
   If the review "passed" in a handful of turns without posting anything on a
   substantive PR, re-run the review job once (`gh run rerun <id>`).
6. Merge (squash): `gh pr merge --squash` once checks pass. The ruleset does
   NOT require branches to be up to date (GitHub's merge queue is unavailable
   on user-owned repos; deploy only fires after CI passes on main, which is
   the safety net for semantic conflicts between merges — a bad merge shows
   red on main and never deploys). When a branch has REAL textual conflicts
   with main, merge `origin/main` into it, resolve the (usually append-append)
   conflicts, and **re-run `npm install`** if the merge brought frontend
   dependency changes — stale `node_modules` throw phantom TS errors.

## 4. Close out the wave

- Remove worker worktrees (`git worktree remove --force`, then prune), delete
  local + remote branches.
- Board cards → *Done* (issues auto-close via `Closes #`).
- Note anything reusable the wave taught (a new gotcha, a convention worth
  adding) — update CLAUDE.md or this skill, not just session memory.

## Known failure modes

- **CI is the arbiter for graph code** — workers without Docker ship Cypher
  that first executes in CI. Prefer `make db-ephemeral` in the worktree when
  Docker is up.
- The review agent is bimodal (shallow pass vs deep pass): rerun shallow
  passes on substantive PRs; findings posted before an `error_max_turns` death
  are still valid.
- Phantom "merge blocked" with all rules green: retry once; the repo-admin
  `--admin` bypass is the sanctioned fallback.
- **A conflicted PR runs NO checks at all.** `pull_request` workflows execute
  against a test merge commit; when the branch conflicts with main, GitHub
  builds no merge commit and every check sits at "Expected — waiting for
  status" forever. This looks exactly like an Actions outage (pushes trigger
  nothing, empty commits and close/reopen don't help, probe branches work
  fine). Check the PR page for "This branch has conflicts" FIRST — the fix is
  simply merging `origin/main` into the branch and resolving. Expect this
  whenever merging one wave PR conflicts the remaining ones (shared hot-file
  appends).
- **Review verdicts live in the LAST PARAGRAPH of the review comment.** Read
  the full comment before merging — a review that "passed" as a check can
  still request changes in its text ("Overall: needs changes"). Never chain
  reading-the-review and `gh pr merge` in one step.

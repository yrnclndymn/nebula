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
5. Triage the review: FIRST enumerate every inline finding as a checklist
   (`gh api .../pulls/N/comments` — not just the summary tail; a three-finding
   round once lost its third finding to a narrow grep and cost an extra
   review cycle). Fix real findings in the worktree (commit + push re-triggers
   checks); rebut with a PR comment where the reviewer is wrong.
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
- **Tag the wave (durable counter, #108).** From an up-to-date `main`, run
  `bash scripts/wave_tag.sh <story-numbers…>` (e.g. `scripts/wave_tag.sh 82 83
  84`). It creates an annotated `wave-NNN` tag (zero-padded, next after the
  highest existing) whose message lists the stories, then prints the push
  command — it never pushes. Push it explicitly:
  `git push origin wave-NNN`. This is the only durable record of how many waves
  have run; `git tag -l 'wave-*'` (or `scripts/wave_tag.sh --count`) counts them
  from any session, offline.
- **Every 3rd wave, run the drift suite.** After tagging: when the new wave's
  NUMBER is a multiple of 3 (`wave-006`, `wave-009`, … — the tag script prints a
  reminder when it creates such a wave; `scripts/wave_tag.sh --count` reports
  the highest wave number for checking from any session), run
  `make drift`. It is read-only and advisory (dead code, dependency freshness,
  secrets, and a module-boundary import graph + a paste-ready LLM prompt). Read
  `scripts/drift-report.txt`, run the modularity prompt through an assistant, and
  **file the findings** as backlog issues / notes (the suite reports; it never
  gates). On the other two-in-three waves, skip it.
- Note anything reusable the wave taught (a new gotcha, a convention worth
  adding) — update CLAUDE.md or this skill, not just session memory.

## Names check

The repo is public and must never carry tracked-company / client names (see the
Guardrails in CLAUDE.md). A deterministic sensor enforces this (#104):
`python3 scripts/check_names.py` pulls the live company+alias list from the graph
at check time (git-ignored cache `.nebula-names-cache`, reused < 24h; never
committed), fuzzy-matches added diff lines + commit messages, and escalates
ambiguous hits to a small Claude model when `ANTHROPIC_API_KEY` is set. A hit
prints file + line + a *redacted* snippet (the matched name is never echoed).

- **Workers:** run `python3 scripts/check_names.py` (staged diff) before handing
  the branch back — a leaked name in a diff or commit message is a blocker, not
  a nit. Install the pre-push hook once per worktree with
  `bash scripts/install-hooks.sh` so it runs automatically.
- **Orchestrator:** the hook fires on every `git push origin <branch>`. Treat a
  block as real — inspect the redacted hunk, fix the source, and re-push. Only
  bypass with `NAMES_CHECK_SKIP="reason"` for a genuine fictional-name collision
  (Acme/Globex) or generic-word false positive; the reason prints loudly.
- Offline with no fresh cache, the check warns and allows (it can't enforce what
  it can't read) — so don't rely on it as the sole gate when disconnected.

## Known failure modes

- **CI is the arbiter for graph code** — workers without Docker ship Cypher
  that first executes in CI. Prefer `make db-ephemeral` in the worktree when
  Docker is up.
- The review agent is multi-modal: (a) deep pass with a verdict; (b)/(c)
  silent pass or placeholder-then-death — since #105 these are SENSED: the
  liveness gate fails the run red and `review-rerun.yml` re-runs it once
  automatically, so don't rerun by hand unless the listener hasn't fired
  (e.g. the sensor isn't on main yet for that PR). (d) attempt 2 also silent —
  the review check stays red BY DESIGN; the orchestrator's own diff review +
  CI carry the merge, via the sanctioned `--admin` (the red required check is
  the conscious-merge signal, not a fault). Findings posted before an
  `error_max_turns` death are still valid.
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
- **Watcher/shell traps:** `gh pr checks --json state -q 'all(...)'` returns
  TRUE on an EMPTY check array (checks not yet registered on a fresh sha) —
  guard with `length > 0`. Never pipe test/lint output (`| tail`) inside an
  `&&`-chain: the pipe masks the exit code and a failure sails through.
- **After scripted conflict resolution, run a delimiter check — eyeballing is
  not enough.** One wave's cascade ate closing braces THREE times (two TS
  interfaces split across hunk boundaries, one CSS rule silently re-nesting a
  shipped section past every linter). After any keep-both swap: assert
  `{`/`}` counts balance per file, run the language's parser (tsc build,
  `ast.parse`, css brace count), and read the boundary lines. CSS especially —
  no gate catches an unclosed rule.
- **Worker reports overstate safety defaults.** "Dry-run by default" must be
  verified at the CLI wrapper (argparse), not the function signature — a
  destructive-by-default maintenance CLI shipped this way. Maintenance CLIs
  standard: dry-run default, `--commit` to apply, invoked via
  `make <target> ARGS=--commit`.
- **CI-only graph flakes: bound the local repro effort.** The schedule/prune
  tests assert GLOBAL counts (`run_tick` enqueues, signals pruned), so any
  stray :Signal/:Company in the shared CI DB fails them with off-by-ones. If
  a CI failure won't reproduce locally after ~2 clean fresh-ephemeral-DB full
  runs (try one keyless: `GEMINI_API_KEY= GOOGLE_API_KEY=` — CI has no LLM
  key), stop digging: push the pending main-merge (or an empty commit) and
  let CI re-run before instrumenting. One such failure cleared on the next
  sha after four clean local repros found nothing.

## Sidecar

A live progress view of the running wave (#107). Start it at launch (step 2),
right after the workers spin up, so the invisible states — silent review,
conflicted-PR-blocks-checks, checks-pending — surface without the orchestrator
narrating. Two commands, from the repo root:

```bash
make wave-watch                              # snapshot every 15s → scripts/wave-status.json
(cd scripts && python3 -m http.server 8777)  # then open localhost:8777/wave_status.html
```

`scripts/wave_status.py` snapshots every `feat/*` branch (local worktrees + open
PRs + PRs merged within `--since-hours`) into one JSON; `wave_status.html` polls
it every 10s. Membership is branch-pattern driven — no registry to maintain, no
per-story wiring. Anomalies render distinctly (dashed badges, not just red/green).
The JSON schema is stable and documented at the top of `wave_status.py`; later,
workers' `make sensors` summaries and the orchestrator's integration gate can
read the same artifact. A one-shot snapshot is `make wave-status`.

**Code-health layer (#124).** `scripts/code_health.py` adds a stdlib-AST sensor
over the *code itself*: per-commit source/test LOC, test ratio, upward imports
vs the #103 lattice, cross-layer edges, and complex/long-function counts.
`python3 scripts/code_health.py --history` walks main's first-parent commits
(via a detached scratch worktree — never your HEAD), caching per-sha into the
git-ignored `scripts/code-health-history.json` so re-runs only scan new commits;
`--rev`/`--path` measure a single tree, `--selftest` is wired into `make lint`.
Each `wave_status.py` story now carries an additive `impact` block — the diff's
layer footprint, guarded-path touches (auth / job dispatch / repository /
proposal-commit), metric deltas vs main, and a `low`/`medium`/`high` risk badge
(high = 3+ code layers or any guarded path). `scripts/code_health.html` polls
both JSONs: stat tiles + one-measure-per-panel trend lines with commit tooltips,
and the in-flight story table with layer chips + risk badges. Serve it alongside
the wave view: `(cd scripts && python3 -m http.server)`.

## Per-wave mutation pass

Coverage (the CI diff-coverage gate) proves changed lines *executed*; it can't
prove the tests would *notice* a bug on those lines. Mutation testing does. It's
too slow for per-PR, so run it once per wave at closeout — the moment new
agent-written tests have actually landed (#106).

At closeout, gather the files the wave touched (backend `app/` only — mutmut
mutates Python source) and run the sensor over them:

```bash
git diff --name-only origin/main...HEAD -- 'backend/app/**/*.py' \
  | sed 's#^backend/##' | tr '\n' ' '            # -> the FILES list
make mutate FILES="app/tools/encoding.py app/graph/signals.py ..."
```

`make mutate` runs mutmut restricted to those files (config in
`backend/pyproject.toml` `[tool.mutmut]`) and prints a compact list of surviving
mutants — mutations no test killed, i.e. real test-quality gaps. Run it with an
ephemeral Neo4j (`make db-ephemeral`, export the URI): without a DB the
graph-gated tests skip and their functions' mutants all report "no tests",
under-reporting exactly the write-path code that matters most. It is a
sensor, NOT a gate: it never runs in CI and doesn't block the merge.

Record the survivors in the wave closeout note (or open follow-up issues for the
worst offenders — a survivor on a security/write-path branch matters more than
one on a log string). A clean run ("none — every executed mutant was killed")
is itself worth recording as the wave's test-quality baseline. Keep the run
scoped to touched files: a whole-tree pass is far too slow to be useful.

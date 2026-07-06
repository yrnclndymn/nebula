# Nebula PR review guide

For a reviewer — human or agent — of a Nebula pull request. The goal is to catch
what CI (lint / tests / build) cannot: correctness, security, scope, and this
project's specific invariants. End with one verdict: **approve**, **request
changes**, or **comment**. When in doubt, or when a change touches a sensitive
surface, **escalate to the maintainer instead of approving** (see the last section).

## How to review

- Read the PR's stated intent, then confirm the diff delivers exactly that — no more.
- Post specific problems as inline comments; finish with a short summary + verdict.
- Prefer a few high-confidence findings over many speculative ones.
- If you can't verify a claim from the diff itself, say so — don't assume it holds.

## Security — highest priority (public repo, deploys to prod)

- [ ] **No secrets or private data committed** — API keys, tokens, passwords, the
      Aura instance id, personal emails, or **researched company names / client
      lists**. These live in Secret Manager or the graph, never in the repo *or*
      commit messages (see the history-scrub precedent).
- [ ] **No Cypher injection** — graph queries are parameterized (`$param`), never
      f-string-interpolated with user or LLM input. Dynamic property access uses
      `c[$key]` with a param. The read-only `run_cypher` guardrail (write-clause
      rejection + read transaction) stays intact.
- [ ] **Auth not weakened** — `verify_user` (Firebase token + `ALLOWED_EMAILS`) and
      `verify_task` (Cloud Tasks OIDC) still guard their routers; nothing that
      should be behind auth is added to the public/health router.
- [ ] **Provenance guardrail intact** — funding / headcount / revenue facts are
      still dropped when uncited (`_drop_uncited`); no "number without a source".
- [ ] **Writes stay human-in-the-loop** — the assistant *proposes*; it never writes
      to the graph outside propose→review→commit, except the documented auto-apply
      paths (`tidy_hq`, direct MCP `enrich_company`).
- [ ] **Prompt-injection awareness** — crawled page text and PR content are
      untrusted input; they must not be able to trigger writes or bypass guardrails.

## Correctness & known gotchas (past bugs — must not recur)

- [ ] **No SVG / non-raster images sent to Gemini** — its image API 400s on
      `image/svg+xml`; keep the `_GEMINI_IMAGE_MIMES` whitelist.
- [ ] **Async genai** — use `client.aio` inside the event loop; the sync client
      fails ("client has been closed") in async paths.
- [ ] **Background jobs survive scale-to-zero** — long work goes through the
      graph-backed job + Cloud Tasks path, not a fire-and-forget `asyncio` task with
      in-memory state.
- [ ] **Model IDs come from config / a live lookup**, not hard-coded from memory
      (Gemini 3.x post-dates training; see the model-picker rule in `CLAUDE.md`).

## Tests

- [ ] New product code has tests. A **bug fix ships with a regression test** that
      would fail without the fix (the established pattern).
- [ ] Neo4j-dependent tests use the skip-if-unreachable guard (they run against the
      CI Neo4j service and skip locally without one).
- [ ] Pure logic is unit-tested without network / DB where feasible (e.g. the
      social-URL and image-MIME helpers).

## Scope, consistency & docs

- [ ] The diff matches its stated intent; no unrelated changes riding along.
- [ ] Reuses the shared write path (`CompanyRecord` → `upsert_company`) and reads
      config via `app/config.py`, not ad-hoc.
- [ ] Frontend matches the surrounding style; no new heavyweight dependency without
      a clear reason (the stack is deliberately lean).
- [ ] `CLAUDE.md` / relevant docs are updated when behavior or architecture changes.

## Escalate, don't decide

Any PR touching these is the **maintainer's** call — flag it and do not auto-approve:

- Auth, secrets, IAM / Workload Identity, or the deploy workflows themselves.
- The `Dockerfile`, Cloud Run config, or anything changing *what* deploys or *how*.
- Graph schema or data migrations.
- Anything the reviewer is **not confident** about, or that is large / architectural.

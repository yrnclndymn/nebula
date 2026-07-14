#!/usr/bin/env python3
"""Tracked-name leak sensor (#104): block commits/pushes that add a tracked name.

The repo is public and the standing guardrail forbids tracked-company / client
names in code, comments, tests, or commit messages — but nothing deterministic
enforced it. This script does. It pulls the *live* company+alias list from the
graph at check time (never storing it in the repo), then scans the staged diff
(or a push range + its commit messages) for added lines that reference a tracked
name.

Two passes:

  1. **Fuzzy** (deterministic, always runs): normalised match of each tracked
     name against each added line, reusing the entity-resolution normalisation
     idea (lowercase, de-punctuate, drop legal-form suffixes) plus the #67
     distinctive-token gate so a company literally named a common word ("Data",
     "Systems") doesn't fire on every line. A whole-name whole-token hit is an
     `exact` match; a distinctive-token subset hit is a `containment` match.
  2. **LLM escalation** (optional, budget-capped): a small fast Claude model
     (`claude-haiku-4-5`) judges the `containment` hunks the fuzzy pass flagged —
     "does this text reference this company (paraphrase counts)?" — so a generic
     coincidence is demoted while a genuine paraphrase is confirmed. `exact`
     hits never go to the LLM (the literal name is right there). The pass is
     skipped gracefully when `ANTHROPIC_API_KEY` is absent, in which case every
     fuzzy hit stands (fail-closed).

CRITICAL output rule: a detection is reported as file + line NUMBER and a
*redacted* snippet (the matched name masked to its first character). The real
name is never echoed into anything that could be pasted into a commit or PR.

Override: set `NAMES_CHECK_SKIP="reason"` to allow the push anyway — the reason
is printed loudly (for fictional-name collisions, generic words, etc.).

Stdlib only, so it runs from the repo root with plain `python3` and needs no
backend deps for the scan itself (only the live pull shells out to
`uv run python -m app.graph.company_names` in `backend/`). The normaliser below
is a deliberate, trimmed copy of `app/graph/entity_resolution.py` — importing
that module from the repo root is awkward (it's a `uv` project rooted at
`backend/`), and duplicating ~40 lines keeps `--selftest` runnable anywhere.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
CACHE_FILE = REPO_ROOT / ".nebula-names-cache"
CACHE_TTL_SECONDS = 24 * 60 * 60

# Small fast Claude model for the escalation pass. Verified current via the
# `claude-api` skill (the Haiku tier); override with NAMES_CHECK_MODEL.
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_LLM_CALLS = 10

COMMIT_SOURCE = "<commit message>"


# --------------------------------------------------------------------------- #
# Normalisation (trimmed copy of app/graph/entity_resolution.py)
# --------------------------------------------------------------------------- #

_LEGAL_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "plc",
        "co", "corp", "corporation", "company", "gmbh", "ag", "kg", "sarl",
        "sa", "sas", "srl", "spa", "bv", "nv", "oy", "ab", "as", "pty", "pte",
        "kk",
    }
)

_STOPWORDS = frozenset({"the", "and"})

_GENERIC_TOKENS = frozenset(
    {
        "north", "south", "east", "west", "northern", "southern", "eastern",
        "western", "central", "national", "international", "global", "united",
        "american", "european", "british", "pacific", "atlantic", "metro",
        "metropolitan", "city", "greater", "royal", "new", "health",
        "healthcare", "care", "medical", "food", "foods", "group", "holding",
        "holdings", "partners", "associates", "services", "service",
        "solutions", "systems", "technologies", "technology", "tech",
        "digital", "data", "media", "capital", "ventures", "financial",
        "finance", "bank", "insurance", "energy", "power", "retail",
        "consulting", "logistics", "industries", "industrial", "enterprises",
        "enterprise", "trust", "council", "authority", "board", "association",
        "foundation", "institute", "network", "labs", "studio", "studios",
        "works",
    }
)

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalized_tokens(name: str) -> list[str]:
    """Lowercase, de-punctuate, drop legal-form suffixes and joiner stopwords."""
    lowered = _PUNCT.sub(" ", name.lower())
    tokens = [t for t in lowered.split() if t and t not in _STOPWORDS]
    stripped = [t for t in tokens if t not in _LEGAL_SUFFIXES]
    return stripped or tokens


def _is_distinctive(token: str) -> bool:
    """A single token strong enough to anchor a match on its own: >= 4 chars
    and not a generic geographic/sector word."""
    return len(token) >= 4 and token not in _GENERIC_TOKENS


def _containment_ok(shared: frozenset[str]) -> bool:
    """Is a subset match strong enough to flag? >= 2 shared tokens, or exactly
    one that is distinctive. A lone generic token never flags (issue #67)."""
    if len(shared) >= 2:
        return True
    if len(shared) == 1:
        return _is_distinctive(next(iter(shared)))
    return False


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def mask_token(token: str) -> str:
    """Mask a token to its first character: 'Globex' -> 'G*****'."""
    if not token:
        return "*"
    return token[0] + "*" * (len(token) - 1)


def redact_line(raw: str, name: str) -> str:
    """Return a snippet of `raw` with the matched name masked.

    Masks the full name and each of its distinctive tokens (case-insensitive).
    As a backstop, if any form of the name survives normalisation of the result
    the whole snippet is hidden rather than risk echoing the name into a log
    that could land in the repo.
    """
    snippet = raw.strip()
    targets = [name] + [t for t in normalized_tokens(name) if _is_distinctive(t)]
    # Longest-first so 'Globex Corp' is masked before 'Globex'. Mask the actual
    # matched text (not the target), so the first char reflects the source and
    # the result is deterministic regardless of target case/order.
    for target in sorted(set(targets), key=len, reverse=True):
        snippet = re.sub(
            re.escape(target),
            lambda mo: mask_token(mo.group(0)),
            snippet,
            flags=re.IGNORECASE,
        )

    name_norm = " ".join(normalized_tokens(name))
    if name_norm and name_norm in " ".join(normalized_tokens(snippet)):
        return "<hidden — snippet still contained the tracked name>"
    if len(snippet) > 120:
        snippet = snippet[:117].rstrip() + "..."
    return snippet


# --------------------------------------------------------------------------- #
# Fuzzy matching
# --------------------------------------------------------------------------- #


def find_matches(names: list[str], entries: list[tuple[str, int, str]]) -> list[dict]:
    """Match tracked `names` against diff `entries` = (source, lineno, raw_text).

    Returns one dict per (entry, name) hit: {source, lineno, name, strength,
    raw}. `strength` is 'exact' (whole name present as whole tokens) or
    'containment' (distinctive-token subset). Names normalising to nothing are
    skipped.
    """
    prepared = []
    for name in names:
        tokens = normalized_tokens(name)
        name_set = frozenset(tokens)
        # A name that isn't distinctive enough to anchor on its own (a lone
        # generic token like "Data" or "Systems") can never be flagged without
        # drowning every line in false positives — the #67 class. Skip it on
        # BOTH the exact and containment paths, not just containment.
        if not tokens or not _containment_ok(name_set):
            continue
        prepared.append((name, name_set, " " + " ".join(tokens) + " "))

    matches: list[dict] = []
    for source, lineno, raw in entries:
        line_tokens = normalized_tokens(raw)
        if not line_tokens:
            continue
        line_set = frozenset(line_tokens)
        line_str = " " + " ".join(line_tokens) + " "
        for name, name_set, name_str in prepared:
            if name_str in line_str:
                strength = "exact"
            elif name_set <= line_set:
                strength = "containment"
            else:
                continue
            matches.append(
                {
                    "source": source,
                    "lineno": lineno,
                    "name": name,
                    "strength": strength,
                    "raw": raw,
                }
            )
    return matches


# --------------------------------------------------------------------------- #
# LLM escalation (optional, budget-capped)
# --------------------------------------------------------------------------- #


def llm_confirm(hunk_text: str, name: str, api_key: str, model: str) -> bool:
    """Ask a small Claude model whether `hunk_text` references company `name`.

    Uses urllib (no `anthropic` dependency). Fail-closed: any error keeps the
    candidate (returns True) so a transport/quota blip never lets a leak
    through. The name and hunk go to the operator's own API key — never printed.
    """
    import json
    import urllib.error
    import urllib.request

    prompt = (
        "You are a precise reviewer preventing one specific company name from "
        "leaking into a PUBLIC code repository.\n\n"
        f"Company: {name}\n\n"
        "Text under review (a diff line or commit message):\n"
        f"{hunk_text}\n\n"
        "Does this text reference that specific company — by name, an obvious "
        "abbreviation, or an unmistakable paraphrase/description of it? Generic "
        "words that merely coincide with the company name do NOT count. Reply "
        "with exactly one word: YES or NO."
    )
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — fail-closed on any failure
        _warn(f"LLM confirmation call failed ({exc}); keeping the candidate")
        return True
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    return text.strip().upper().startswith("Y")


def escalate(matches: list[dict], api_key: str | None, model: str) -> list[dict]:
    """Apply the LLM pass to 'containment' matches; return the kept matches.

    'exact' matches always stand. Without a key the whole pass is skipped and
    every fuzzy match stands (fail-closed). With a key, up to MAX_LLM_CALLS
    containment matches are confirmed; a 'no' demotes that match, a 'yes' keeps
    it. Any containment matches beyond the budget stand (fail-closed) with a
    warning.
    """
    exact = [m for m in matches if m["strength"] == "exact"]
    containment = [m for m in matches if m["strength"] == "containment"]

    if not containment:
        return exact
    if not api_key:
        _warn(
            "ANTHROPIC_API_KEY not set — skipping the LLM paraphrase check; "
            f"{len(containment)} fuzzy hit(s) stand as-is."
        )
        return exact + containment

    kept = list(exact)
    calls = 0
    for match in containment:
        if calls >= MAX_LLM_CALLS:
            _warn(
                f"LLM budget ({MAX_LLM_CALLS}) exhausted — remaining "
                "containment hit(s) stand unconfirmed."
            )
            kept.append(match)
            continue
        calls += 1
        if llm_confirm(match["raw"], match["name"], api_key, model):
            kept.append(match)
    return kept


# --------------------------------------------------------------------------- #
# Diff / range collection
# --------------------------------------------------------------------------- #


def _run_git(args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    return out.stdout if out.returncode == 0 else ""


def parse_added_lines(diff: str) -> list[tuple[str, int, str]]:
    """Parse a unified diff into (file, new_lineno, added_text) entries."""
    entries: list[tuple[str, int, str]] = []
    current_file: str | None = None
    new_ln = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current_file = None if path == "/dev/null" else path
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_ln = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if current_file is not None:
                entries.append((current_file, new_ln, line[1:]))
            new_ln += 1
        elif line.startswith("-") and not line.startswith("---"):
            pass  # removed line — does not advance the new-file counter
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            new_ln += 1  # context line
    return entries


def _commit_message_entries(rng: str) -> list[tuple[str, int, str]]:
    body = _run_git(["log", rng, "--format=%B"])
    entries = []
    for line in body.splitlines():
        if line.strip():
            entries.append((COMMIT_SOURCE, 0, line))
    return entries


def collect_staged() -> list[tuple[str, int, str]]:
    return parse_added_lines(_run_git(["diff", "--cached"]))


def collect_range(rng: str) -> list[tuple[str, int, str]]:
    # Accept "base..head" or "base...head"; use two-dot for the tree diff.
    base_head = rng.replace("...", "..")
    entries = parse_added_lines(_run_git(["diff", base_head]))
    entries += _commit_message_entries(base_head)
    return entries


def _is_zero(sha: str) -> bool:
    return sha == "" or set(sha) <= {"0"}


def _empty_tree() -> str:
    return _run_git(["hash-object", "-t", "tree", os.devnull]).strip()


def collect_pre_push(stdin_text: str) -> list[tuple[str, int, str]]:
    """Collect added text for a pre-push hook from git's stdin protocol.

    Each stdin line: `<local_ref> <local_sha> <remote_ref> <remote_sha>`. For a
    branch already on the remote the range is `remote_sha..local_sha`; for a new
    branch we fall back to origin/main's merge-base (or the empty tree).
    """
    entries: list[tuple[str, int, str]] = []
    seen_lines = False
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        seen_lines = True
        _local_ref, local_sha, _remote_ref, remote_sha = parts[:4]
        if _is_zero(local_sha):
            continue  # deleting a remote branch — nothing to scan
        if not _is_zero(remote_sha):
            base = remote_sha
        else:
            base = _run_git(["merge-base", "origin/main", local_sha]).strip()
            if not base:
                base = _empty_tree()
        entries += collect_range(f"{base}..{local_sha}")
    if not seen_lines:
        # Invoked manually (no stdin): fall back to the tracked upstream.
        base = _run_git(["rev-parse", "--verify", "--quiet", "@{u}"]).strip()
        if not base:
            base = _run_git(["rev-parse", "--verify", "--quiet", "origin/main"]).strip()
        head = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"]).strip()
        if base and head:
            entries += collect_range(f"{base}..{head}")
        else:
            entries += collect_staged()
    return entries


# --------------------------------------------------------------------------- #
# Name list (live pull + git-ignored cache)
# --------------------------------------------------------------------------- #


def _read_cache() -> tuple[float, list[str]] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        lines = CACHE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        ts = float(lines[0])
    except ValueError:
        return None
    return ts, [n for n in lines[1:] if n.strip()]


def _write_cache(names: list[str]) -> None:
    try:
        CACHE_FILE.write_text(
            "\n".join([str(time.time()), *names]) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # noqa: BLE001
        _warn(f"could not write name cache ({exc})")


# Out-of-tree env file holding READ-ONLY production Aura creds for this check.
# The guardrail protects the PRODUCTION tracked list — pulling from the local dev
# DB would check against fictional test data (or nothing), which inverts the
# safety goal. Real env vars still win over the file, and the file must never
# live inside the repo.
ENV_FILE = Path.home() / ".nebula" / "names-check.env"


def _check_env() -> tuple[dict, bool]:
    """The env for the names pull (process env + ENV_FILE defaults), and whether
    the source looks like production (Aura `neo4j+s://` scheme)."""
    env = os.environ.copy()
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    uri = env.get("NEO4J_URI", "")
    return env, uri.startswith("neo4j+s://")


def _pull_live_names() -> tuple[list[str] | None, bool]:
    env, prod = _check_env()
    if not prod:
        _warn(
            "names list source is NOT the production graph (as far as this hook "
            f"can tell) — put read-only Aura creds in {ENV_FILE} "
            "(NEO4J_URI=neo4j+s://… NEO4J_USER=… NEO4J_PASSWORD=…) so the check "
            "guards the real tracked list, not local test data."
        )
    try:
        out = subprocess.run(
            ["uv", "run", "python", "-m", "app.graph.company_names"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _warn(f"live name pull failed ({exc})")
        return None, prod
    if out.returncode != 0:
        # One line, not a traceback dump — the last stderr line carries the cause.
        tail = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "unknown error"
        _warn(f"live name pull failed: {tail}")
        return None, prod
    return [n for n in out.stdout.splitlines() if n.strip()], prod


def load_names() -> list[str] | None:
    """The tracked-name list, from a fresh cache (<24h) or a live pull.

    Returns None when no list can be obtained at all (offline, no cache) — the
    caller then warns and passes, since it cannot enforce what it cannot read.
    The list is never written anywhere but the git-ignored cache file.
    """
    cached = _read_cache()
    if cached and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    live, _prod = _pull_live_names()
    if live is not None:
        _write_cache(live)
        return live

    if cached is not None:
        _warn("using a stale name cache (live pull failed).")
        return cached[1]

    return None


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def _warn(msg: str) -> None:
    sys.stderr.write(f"names-check: warning: {msg}\n")


def report(matches: list[dict]) -> None:
    sys.stderr.write("\n" + "=" * 70 + "\n")
    sys.stderr.write("names-check: possible tracked-name leak(s) detected\n")
    sys.stderr.write("=" * 70 + "\n")
    for m in matches:
        loc = m["source"] if m["lineno"] == 0 else f"{m['source']}:{m['lineno']}"
        sys.stderr.write(f"  {loc}  [{m['strength']}]  {redact_line(m['raw'], m['name'])}\n")
    sys.stderr.write(
        "\nThe matched name is masked above and never printed in full.\n"
        "If this is a false positive (a fictional fixture name that collides, a\n"
        'generic word), re-run the push with NAMES_CHECK_SKIP="your reason".\n'
    )


# --------------------------------------------------------------------------- #
# Selftest (pure logic, fictional names only — the very rule this enforces)
# --------------------------------------------------------------------------- #


def selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # Normalisation
    check(normalized_tokens("The Globex Company") == ["globex"], "norm strips legal+stop")
    check(normalized_tokens("Acme, LLC") == ["acme"], "norm strips punctuation+suffix")

    names = ["Globex", "Data Systems", "Initech Labs", "Systems", "Acme"]

    # Diff parsing: file + line number tracking
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,2 +1,3 @@\n"
        " context\n"
        "+we use Globex for billing\n"
        "-old removed line\n"
        "+ordinary data pipeline code\n"
    )
    entries = parse_added_lines(diff)
    check(("f.py", 2, "we use Globex for billing") in entries, "diff parse file+lineno")
    check(("f.py", 3, "ordinary data pipeline code") in entries, "diff parse after removal")

    matches = find_matches(names, entries)
    by_name = {(m["name"], m["strength"]) for m in matches}
    check(("Globex", "exact") in by_name, "distinctive full name -> exact")
    # 'data' alone is generic and 'Data Systems' needs both tokens -> no hit.
    check(all(m["name"] != "Data Systems" for m in matches), "generic multi-token no false hit")
    check(all(m["name"] != "Systems" for m in matches), "lone generic token no hit")
    # A lone generic name must not fire even as a verbatim substring (exact path).
    check(find_matches(["Data"], [("f", 1, "process the data now")]) == [],
          "lone generic token no exact hit")

    # Non-contiguous tokens -> containment (contiguous would be an exact hit).
    initech_entries = [("x", 5, "Initech scaled up its Labs unit")]
    im = find_matches(names, initech_entries)
    check(any(m["name"] == "Initech Labs" and m["strength"] == "containment" for m in im),
          "distinctive+generic subset -> containment")

    acme_entries = [("y", 1, "the Acme Corp integration")]
    am = find_matches(names, acme_entries)
    check(any(m["name"] == "Acme" and m["strength"] == "exact" for m in am), "short name exact")

    # Redaction never echoes the name
    red = redact_line("client is Globex Inc here", "Globex")
    check("globex" not in red.lower(), "redaction masks the name")
    check(red.startswith("client is G"), "redaction keeps context + first char")
    check(mask_token("Globex") == "G*****", "mask_token shape")

    if failures:
        sys.stderr.write("SELFTEST FAILURES:\n")
        for f in failures:
            sys.stderr.write(f"  - {f}\n")
        return 1
    print("names-check selftest: OK")
    return 0


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan `git diff --cached` (default)")
    mode.add_argument("--range", metavar="BASE..HEAD", help="scan a commit range + its messages")
    mode.add_argument("--pre-push", action="store_true", help="read git's pre-push stdin protocol")
    mode.add_argument("--selftest", action="store_true", help="run pure-logic self-checks and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    skip = os.environ.get("NAMES_CHECK_SKIP")
    if skip:
        sys.stderr.write(
            "\n" + "!" * 70 + "\n"
            f"names-check SKIPPED by NAMES_CHECK_SKIP: {skip}\n"
            + "!" * 70 + "\n"
        )
        return 0

    if args.range:
        entries = collect_range(args.range)
    elif args.pre_push:
        entries = collect_pre_push(sys.stdin.read())
    else:
        entries = collect_staged()

    if not entries:
        return 0

    names = load_names()
    if names is None:
        _warn(
            "no tracked-name list available (offline and no cache) — cannot "
            "enforce the name check; allowing."
        )
        return 0

    matches = find_matches(names, entries)
    if not matches:
        return 0

    model = os.environ.get("NAMES_CHECK_MODEL", DEFAULT_MODEL)
    matches = escalate(matches, os.environ.get("ANTHROPIC_API_KEY"), model)
    if not matches:
        return 0

    report(matches)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Code-health sensor (#124) — stdlib-only AST metrics over any git tree.

A companion to the wave sidecar (#107) and the drift suite (#108). Where those
watch the *process* (PR state, dead code, deps), this watches the *shape of the
code itself* and how it moves commit-to-commit: how much source vs test, how
many functions have grown complex or long, and — the reason it exists — whether
the module layering (the #103 lattice) is eroding via upward imports.

Design mirrors `drift.py`: stdlib only, pure AST, no backend deps, no network,
no LLM. It measures a *tree* (a checked-out working directory), so history and
in-flight branches are measured by exactly the same code path — the only
difference is which tree we point it at.

Modes:
  --path DIR        analyze one working directory (default: repo root)
  --rev REV         analyze one git rev (checked out into a throwaway worktree)
  --history         walk main's first-parent commits, caching per-sha into
                    scripts/code-health-history.json (only new commits scanned),
                    and emit the ordered series the code_health.html view polls
  --selftest        run the pure-function unit checks (wired into `make lint`)

The layering model is a deliberately coarse projection of the import-linter
contract in backend/pyproject.toml:

    foundation  <  graph  <  tools  <  capture/agents  <  surfaces

Higher layers may import lower ones; a LOWER layer importing a HIGHER one is an
"upward" import (a layering violation). Lazy (function-local) imports never
count — they are the sanctioned dispatch/escape hatch (see the contract's
`ignore_imports`), so counting them would just flag the exceptions on purpose.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
HISTORY_CACHE = SCRIPTS_DIR / "code-health-history.json"
SCHEMA_VERSION = 1

# Function-quality thresholds (a function over either is "hot").
CYCLO_THRESHOLD = 12   # cyclomatic complexity
LINES_THRESHOLD = 60   # physical line span

# --- Layer lattice (low -> high), mirroring backend/pyproject.toml's #103 ------
# import-linter contract. It is a coarse projection: the contract splits
# agents.assistant from the other agent domains, but for a health *trend* one
# "capture/agents" band is enough. Buckets not listed don't participate.
LAYERS = ["foundation", "graph", "tools", "capture/agents", "surfaces"]
LAYER_RANK = {name: i for i, name in enumerate(LAYERS)}

BUCKET_LAYER = {
    "config": "foundation",
    "auth": "foundation",
    "budget": "foundation",
    "ratelimit": "foundation",
    "genai_retry": "foundation",
    "graph": "graph",
    "tools": "tools",
    "agents": "capture/agents",
    "capture": "capture/agents",
    "api": "surfaces",
    "mcp_server": "surfaces",
    "importer": "surfaces",
    "main": "surfaces",
}

# Metric keys whose deltas wave_status.py reports (numeric only).
METRIC_KEYS = [
    "source_loc",
    "test_loc",
    "test_count",
    "upward_imports",
    "cross_layer_edges",
    "complex_functions",
    "long_functions",
    "noqa_count",
    "test_ratio",
]


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested via --selftest — no git/files/network needed)
# ----------------------------------------------------------------------------

def layer_of_bucket(bucket: str) -> str | None:
    """Top-level app bucket -> layer name, or None if it isn't in the lattice."""
    return BUCKET_LAYER.get(bucket)


def layer_for_path(rel_path: str) -> str | None:
    """A repo-relative path -> its code layer, or None for non-app / tooling.

    backend/app/auth.py        -> 'foundation'  (top-level module file)
    backend/app/graph/x.py     -> 'graph'
    backend/app/agents/y/z.py  -> 'capture/agents'
    frontend/... , scripts/... -> None (not a backend code layer)
    """
    parts = Path(rel_path).parts
    if len(parts) >= 3 and parts[0] == "backend" and parts[1] == "app":
        bucket = parts[2]
        if bucket.endswith(".py"):
            bucket = bucket[: -len(".py")]
        return layer_of_bucket(bucket)
    return None


def count_code_loc(source: str) -> int:
    """Physical code lines: non-blank and not a whole-line comment."""
    n = 0
    for line in source.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        n += 1
    return n


def count_noqa(source: str) -> int:
    """Count `# noqa` suppressions (a small, honest debt signal)."""
    return sum(1 for line in source.splitlines()
               if "# noqa" in line or "#noqa" in line)


def _iter_body_no_nested(func: ast.AST):
    """Yield nodes in a function's body, NOT descending into nested defs/lambdas.

    Nested functions are measured on their own (ast.walk visits them separately);
    a lambda's body is intentionally not scored. Comprehensions ARE descended
    into, so their `if` clauses can be counted.
    """
    stack = list(getattr(func, "body", []))
    while stack:
        node = stack.pop()
        yield node
        # Don't descend into a nested scope — it is scored on its own.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def cyclomatic(func: ast.AST) -> int:
    """Cyclomatic complexity: 1 + decision points.

    Decision points = If / For / AsyncFor / While / ExceptHandler / BoolOp /
    IfExp, plus each `if` clause inside a comprehension. Matches the backfill
    analyzer that proved these metrics over history.
    """
    count = 1
    for node in _iter_body_no_nested(func):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While,
                             ast.ExceptHandler, ast.BoolOp, ast.IfExp)):
            count += 1
        elif isinstance(node, ast.comprehension):
            count += len(node.ifs)
    return count


def function_span(func: ast.AST) -> int:
    """Physical line span of a function (def line through its last line)."""
    start = getattr(func, "lineno", None)
    end = getattr(func, "end_lineno", None)
    if start is None or end is None:
        return 0
    return end - start + 1


def _module_parts(py_file: Path, app_root: Path) -> list[str]:
    """['graph', 'driver'] for <app_root>/graph/driver.py (drops 'app'/__init__)."""
    rel = py_file.relative_to(app_root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def bucket_for_module(mod_parts: list[str]) -> str:
    """Map app.* module parts (without leading 'app') to their top-level bucket."""
    return mod_parts[0] if mod_parts else "app"


def resolve_import(node, src_parts: list[str], is_submodule):
    """Yield resolved app.* dotted targets (relative to 'app') for one import.

    `is_submodule(parts)` decides whether `app.<parts>` names a real module —
    injected so the pure layer is testable without a filesystem. Logic mirrors
    drift.py: absolute `app.x`, relative `.`/`..`, and `from app.pkg import name`
    resolving to the submodule when it exists.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            dotted = alias.name.split(".")
            if dotted and dotted[0] == "app":
                yield dotted[1:]
        return

    # ImportFrom
    if node.level and node.level > 0:
        pkg = list(src_parts[:-1]) if src_parts else []
        climb = node.level - 1
        if climb > 0:
            pkg = pkg[: max(0, len(pkg) - climb)]
        mod_parts = pkg + (node.module.split(".") if node.module else [])
    elif node.module and node.module.split(".")[0] == "app":
        mod_parts = node.module.split(".")[1:]
    else:
        return

    emitted = False
    for alias in node.names:
        candidate = mod_parts + [alias.name]
        if is_submodule(candidate):
            yield candidate
            emitted = True
    if not emitted:
        yield mod_parts


def _lazy_import_ids(tree: ast.AST) -> set[int]:
    """id()s of import nodes nested inside a function/method (lazy imports)."""
    lazy: set[int] = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(fn):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    lazy.add(id(inner))
    return lazy


def story_risk(n_code_layers: int, guarded: bool) -> str:
    """Risk badge for an in-flight story's footprint (ONE pure heuristic — #124).

    high   = touches a guarded path OR spreads across 3+ code layers
    medium = spreads across exactly 2 code layers
    low    = 1 code layer, or tooling-only (0 code layers)

    Guarded wins outright: a one-line auth/write-path change is riskier than a
    broad but shallow tooling sweep. Deliberately blunt — it will get tuned.
    """
    if guarded or n_code_layers >= 3:
        return "high"
    if n_code_layers == 2:
        return "medium"
    return "low"


# ----------------------------------------------------------------------------
# Tree analysis (impure only in that it reads files off a working directory)
# ----------------------------------------------------------------------------

def _make_is_submodule(app_root: Path):
    def is_submodule(parts: list[str]) -> bool:
        base = app_root.joinpath(*parts)
        return base.with_suffix(".py").exists() or (base / "__init__.py").exists()
    return is_submodule


def _read(py: Path) -> str:
    try:
        return py.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _iter_py(root: Path):
    if not root.exists():
        return
    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        yield py


def analyze_tree(root: str | Path) -> dict:
    """Compute the health metrics for one working directory.

    `root` is a repo root (contains backend/app + backend/tests). Missing dirs
    yield zeros, so old commits that predate the current layout measure cleanly.
    """
    root = Path(root)
    app_root = root / "backend" / "app"
    tests_root = root / "backend" / "tests"
    is_submodule = _make_is_submodule(app_root)

    source_loc = complex_functions = long_functions = noqa_count = upward = 0
    cross_layer: set[tuple[str, str]] = set()

    for py in _iter_py(app_root):
        src = _read(py)
        source_loc += count_code_loc(src)
        noqa_count += count_noqa(src)
        try:
            tree = ast.parse(src, filename=str(py))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if cyclomatic(node) > CYCLO_THRESHOLD:
                    complex_functions += 1
                if function_span(node) > LINES_THRESHOLD:
                    long_functions += 1

        src_parts = _module_parts(py, app_root)
        src_layer = layer_of_bucket(bucket_for_module(src_parts))
        if src_layer is None:
            continue
        lazy_ids = _lazy_import_ids(tree)
        for imp in ast.walk(tree):
            if not isinstance(imp, (ast.Import, ast.ImportFrom)):
                continue
            if id(imp) in lazy_ids:
                continue
            for target in resolve_import(imp, src_parts, is_submodule):
                dst_bucket = bucket_for_module(target)
                dst_layer = layer_of_bucket(dst_bucket)
                if dst_layer is None or dst_layer == src_layer:
                    continue
                cross_layer.add((bucket_for_module(src_parts), dst_bucket))
                if LAYER_RANK[dst_layer] > LAYER_RANK[src_layer]:
                    upward += 1  # occurrence count: a LOWER layer reaching UP

    test_loc = test_count = 0
    for py in _iter_py(tests_root):
        src = _read(py)
        test_loc += count_code_loc(src)
        try:
            tree = ast.parse(src, filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test")):
                test_count += 1

    test_ratio = round(test_loc / source_loc, 4) if source_loc else 0.0

    return {
        "source_loc": source_loc,
        "test_loc": test_loc,
        "test_count": test_count,
        "upward_imports": upward,
        # distinct directed bucket->bucket edges that cross a layer boundary
        "cross_layer_edges": len(cross_layer),
        "complex_functions": complex_functions,
        "long_functions": long_functions,
        "noqa_count": noqa_count,
        "test_ratio": test_ratio,
    }


# ----------------------------------------------------------------------------
# Git plumbing for --rev / --history (detached scratch worktree; never HEAD)
# ----------------------------------------------------------------------------

def _git(args: list[str], cwd: Path, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=timeout, check=True,
    )
    return proc.stdout


def first_parent_commits(repo_root: Path, branch: str = "main") -> list[dict]:
    """First-parent commits of `branch`, oldest-first: {sha, short, ts, subject}."""
    fmt = "%H%x1f%ct%x1f%s"
    out = _git(["log", "--first-parent", f"--format={fmt}", branch], repo_root)
    commits: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, ts, subject = (line.split("\x1f", 2) + ["", ""])[:3]
        commits.append({
            "sha": sha,
            "short": sha[:9],
            "ts": int(ts) if ts.isdigit() else 0,
            "subject": subject,
        })
    commits.reverse()  # oldest-first so the trend reads left->right
    return commits


def analyze_rev(repo_root: Path, rev: str) -> dict:
    """Analyze a single git rev via a throwaway detached worktree."""
    _git(["worktree", "prune"], repo_root)
    scratch = Path(tempfile.mkdtemp(prefix="nebula-health-"))
    try:
        _git(["worktree", "add", "--detach", str(scratch), rev], repo_root)
        return analyze_tree(scratch)
    finally:
        _git(["worktree", "remove", "--force", str(scratch)], repo_root)
        _git(["worktree", "prune"], repo_root)


def load_history_cache() -> dict:
    """Return {sha: entry}. Robust to a missing/corrupt cache file."""
    if not HISTORY_CACHE.exists():
        return {}
    try:
        data = json.loads(HISTORY_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {c["sha"]: c for c in data.get("commits", []) if "sha" in c}


def build_history(repo_root: Path, branch: str = "main") -> dict:
    """Scan main's first-parent commits, caching per-sha; return the payload."""
    commits = first_parent_commits(repo_root, branch)
    cache = load_history_cache()
    todo = [c for c in commits if c["sha"] not in cache]

    scanned = 0
    if todo:
        _git(["worktree", "prune"], repo_root)
        scratch = Path(tempfile.mkdtemp(prefix="nebula-health-"))
        try:
            _git(["worktree", "add", "--detach", str(scratch), todo[0]["sha"]],
                 repo_root)
            for c in todo:
                _git(["checkout", "--detach", c["sha"]], scratch)
                metrics = analyze_tree(scratch)
                cache[c["sha"]] = {
                    "sha": c["sha"],
                    "short": c["short"],
                    "ts": c["ts"],
                    "iso": datetime.fromtimestamp(c["ts"], timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ") if c["ts"] else None,
                    "subject": c["subject"],
                    "metrics": metrics,
                }
                scanned += 1
        finally:
            _git(["worktree", "remove", "--force", str(scratch)], repo_root)
            _git(["worktree", "prune"], repo_root)

    ordered = [cache[c["sha"]] for c in commits if c["sha"] in cache]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "branch": branch,
        "commits": ordered,
    }
    HISTORY_CACHE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"payload": payload, "scanned": scanned, "total": len(ordered)}


# ----------------------------------------------------------------------------
# Self-test (embedded fixtures — pure layer only)
# ----------------------------------------------------------------------------

def _selftest() -> int:
    failures: list[str] = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # -- layer mapping + lattice order ---------------------------------------
    check("layer config", layer_of_bucket("config"), "foundation")
    check("layer auth", layer_of_bucket("auth"), "foundation")
    check("layer graph", layer_of_bucket("graph"), "graph")
    check("layer tools", layer_of_bucket("tools"), "tools")
    check("layer agents", layer_of_bucket("agents"), "capture/agents")
    check("layer capture", layer_of_bucket("capture"), "capture/agents")
    check("layer api", layer_of_bucket("api"), "surfaces")
    check("layer unknown", layer_of_bucket("nope"), None)
    check("lattice order",
          LAYER_RANK["foundation"] < LAYER_RANK["graph"] < LAYER_RANK["tools"]
          < LAYER_RANK["capture/agents"] < LAYER_RANK["surfaces"], True)

    # -- path -> layer (wave_status footprint) -------------------------------
    check("path auth", layer_for_path("backend/app/auth.py"), "foundation")
    check("path graph", layer_for_path("backend/app/graph/x.py"), "graph")
    check("path agents", layer_for_path("backend/app/agents/people/build.py"),
          "capture/agents")
    check("path main", layer_for_path("backend/app/main.py"), "surfaces")
    check("path frontend", layer_for_path("frontend/src/App.tsx"), None)
    check("path scripts", layer_for_path("scripts/code_health.py"), None)

    # -- LOC + noqa -----------------------------------------------------------
    src = "import os\n\n# a comment\nx = 1\n   \ny = 2  # noqa: E501\n"
    check("code loc", count_code_loc(src), 3)     # import, x=1, y=2
    check("noqa count", count_noqa(src), 1)

    # -- cyclomatic + span ----------------------------------------------------
    fixture = (
        "def f(a, b):\n"
        "    if a and b:\n"           # If (+1) + BoolOp (+1)
        "        return 1\n"
        "    for i in range(a):\n"    # For (+1)
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"    # ExceptHandler (+1)
        "        pass\n"
        "    xs = [i for i in range(a) if i > 1]\n"  # comprehension if (+1)
        "    return b if a else 0\n"  # IfExp (+1)
    )
    fn = ast.parse(fixture).body[0]
    check("cyclomatic", cyclomatic(fn), 1 + 6)
    check("function span", function_span(fn), 11)

    # nested def not double-counted in the outer function's complexity
    nested = (
        "def outer(a):\n"
        "    def inner(b):\n"
        "        if b:\n"
        "            return 1\n"
        "        return 0\n"
        "    if a:\n"
        "        return inner(a)\n"
        "    return 0\n"
    )
    outer = ast.parse(nested).body[0]
    inner = outer.body[0]
    check("outer cyclo", cyclomatic(outer), 1 + 1)   # only outer's `if a`
    check("inner cyclo", cyclomatic(inner), 1 + 1)   # only inner's `if b`

    # -- import resolution + bucketing ---------------------------------------
    def resolve(src_mod: str, code: str, submods=frozenset()):
        tree = ast.parse(code)
        src_parts = src_mod.split(".") if src_mod else []

        def is_sub(parts):
            return ".".join(parts) in submods

        results = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in resolve_import(node, src_parts, is_sub):
                    results.append(bucket_for_module(target))
        return sorted(results)

    check("abs from", resolve("api.routes", "from app.graph.driver import x"),
          ["graph"])
    check("abs import", resolve("main", "import app.tools.web"), ["tools"])
    check("stdlib ignored", resolve("main", "import os\nfrom typing import List"),
          [])
    check("from app submodule",
          resolve("tools.web", "from app import budget, ratelimit",
                  submods={"budget", "ratelimit"}),
          ["budget", "ratelimit"])
    check("relative sibling",
          resolve("graph.jobs", "from .driver import get_driver"), ["graph"])

    # upward vs downward classification (the metric that matters)
    def is_upward(src_bucket, dst_bucket):
        sl, dl = layer_of_bucket(src_bucket), layer_of_bucket(dst_bucket)
        return LAYER_RANK[dl] > LAYER_RANK[sl]
    check("graph->tools upward", is_upward("graph", "tools"), True)
    check("api->graph downward", is_upward("api", "graph"), False)

    # -- risk heuristic -------------------------------------------------------
    check("risk 0 layers", story_risk(0, False), "low")
    check("risk 1 layer", story_risk(1, False), "low")
    check("risk 2 layers", story_risk(2, False), "medium")
    check("risk 3 layers", story_risk(3, False), "high")
    check("risk guarded overrides", story_risk(2, True), "high")
    check("risk guarded tooling", story_risk(0, True), "high")

    if failures:
        print("CODE-HEALTH SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("code_health selftest OK (pure AST + risk assertions passed)")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _print_trend(payload: dict) -> None:
    commits = payload.get("commits", [])
    if not commits:
        print("(no commits)")
        return
    latest = commits[-1]["metrics"]
    print(f"latest ({commits[-1]['short']}): "
          f"src={latest['source_loc']} test={latest['test_loc']} "
          f"ratio={latest['test_ratio']} upward={latest['upward_imports']} "
          f"cross={latest['cross_layer_edges']} "
          f"complex={latest['complex_functions']} long={latest['long_functions']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--selftest", action="store_true",
                   help="run pure-function unit checks and exit")
    g.add_argument("--history", action="store_true",
                   help="walk main's first-parent commits (cached) -> history JSON")
    g.add_argument("--rev", help="analyze a single git rev")
    g.add_argument("--path", help="analyze a single working directory")
    parser.add_argument("--branch", default="main",
                        help="branch for --history (default: main)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.history:
        result = build_history(REPO_ROOT, args.branch)
        print(f"wrote {HISTORY_CACHE.relative_to(REPO_ROOT)} — "
              f"{result['total']} commits ({result['scanned']} newly scanned)")
        _print_trend(result["payload"])
        return 0

    if args.rev:
        metrics = analyze_rev(REPO_ROOT, args.rev)
    else:
        metrics = analyze_tree(args.path or REPO_ROOT)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

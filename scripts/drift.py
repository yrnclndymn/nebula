#!/usr/bin/env python3
"""Drift suite (#108) — slow-cadence, read-only sensors run every 3rd wave.

Unlike the per-push gates (lint, tests, tracked-name sensor), these are advisory:
they surface *drift* that accretes across many waves — dead code, stale deps,
leaked secrets, and eroding module boundaries. Output is a single readable
report, not a gate. Nothing here writes to the repo, the graph, or the network
beyond the read-only tools it shells out to, and it never calls an LLM (the
modularity section emits a graph + a paste-ready prompt for a human/orchestrator
to run separately, so `make drift` stays deterministic and free).

Every section is best-effort: a missing tool prints a clear SKIPPED line with an
install hint rather than failing the run. The combined report is streamed to
stdout and also written to scripts/drift-report.txt (git-ignored).

Run: `make drift` (or `python3 scripts/drift.py`).
"""

from __future__ import annotations

import ast
import io
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_APP = REPO_ROOT / "backend" / "app"
FRONTEND = REPO_ROOT / "frontend"
REPORT_PATH = REPO_ROOT / "scripts" / "drift-report.txt"


class Tee(io.StringIO):
    """Write to stdout as we go AND accumulate for the on-disk report."""

    def write(self, s: str) -> int:  # type: ignore[override]
        sys.stdout.write(s)
        return super().write(s)


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run a read-only command, capturing combined output. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        return 127, f"(command not found: {cmd[0]})"
    except subprocess.TimeoutExpired:
        return 124, "(timed out after 300s)"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _section(out: Tee, title: str) -> None:
    out.write("\n" + "=" * 72 + "\n")
    out.write(f"  {title}\n")
    out.write("=" * 72 + "\n")


def _skipped(out: Tee, reason: str, hint: str | None = None) -> None:
    out.write(f"SKIPPED — {reason}\n")
    if hint:
        out.write(f"  hint: {hint}\n")


# ---------------------------------------------------------------------------
# 1. Dead code — vulture via uvx (no dependency changes)
# ---------------------------------------------------------------------------
def section_dead_code(out: Tee) -> None:
    _section(out, "1. Dead code (vulture, confidence >= 90)")
    if not shutil.which("uvx"):
        _skipped(out, "uvx not on PATH", "install uv: https://docs.astral.sh/uv/")
        return
    code, output = _run(
        ["uvx", "vulture", "backend/app", "--min-confidence", "90"],
        cwd=REPO_ROOT,
    )
    if code == 127:
        _skipped(out, "could not launch uvx/vulture", "check `uvx vulture --help`")
        return
    if output:
        out.write(output + "\n")
    if not output.strip():
        out.write("clean — no high-confidence dead code.\n")


# ---------------------------------------------------------------------------
# 2. Dependency freshness — uv (backend) + npm (frontend)
# ---------------------------------------------------------------------------
def section_dep_freshness(out: Tee) -> None:
    _section(out, "2. Dependency freshness")

    out.write("\n-- backend (uv) --\n")
    if not shutil.which("uv"):
        _skipped(out, "uv not on PATH", "install uv: https://docs.astral.sh/uv/")
    else:
        code, output = _run(["uv", "pip", "list", "--outdated"], cwd=REPO_ROOT / "backend")
        if code != 0 and ("unrecognized" in output or "error" in output.lower()):
            # Fall back to the tree view on older/newer uv where the flag differs.
            code, output = _run(["uv", "tree", "--outdated"], cwd=REPO_ROOT / "backend")
        out.write((output or "(no output)") + "\n")

    out.write("\n-- frontend (npm) --\n")
    if not shutil.which("npm"):
        _skipped(out, "npm not on PATH", "install Node.js")
    elif not (FRONTEND / "node_modules").exists():
        _skipped(out, "frontend/node_modules missing", "run `make frontend-install`")
    else:
        # npm outdated exits 1 when anything is outdated — that's expected, not a failure.
        _, output = _run(["npm", "outdated"], cwd=FRONTEND)
        out.write((output or "up to date — nothing outdated.") + "\n")


# ---------------------------------------------------------------------------
# 3. Secrets — gitleaks (distinct from the tracked-name sensor)
# ---------------------------------------------------------------------------
def section_secrets(out: Tee) -> None:
    _section(out, "3. Secret scan (gitleaks)")
    if not shutil.which("gitleaks"):
        _skipped(out, "gitleaks not installed", "brew install gitleaks")
        return
    code, output = _run(["gitleaks", "detect", "--no-banner"], cwd=REPO_ROOT)
    if output:
        out.write(output + "\n")
    if code == 0:
        out.write("clean — no leaks detected.\n")
    else:
        out.write(f"gitleaks exited {code} — review findings above.\n")


# ---------------------------------------------------------------------------
# 4. Modularity — stdlib AST import graph + paste-ready LLM prompt
# ---------------------------------------------------------------------------
def _bucket_for_module(mod_parts: list[str]) -> str:
    """Map an app.* module to its top-level bucket.

    app/auth.py            -> app.auth            -> 'auth'   (top-level module)
    app/graph/driver.py    -> app.graph.driver    -> 'graph'  (subpackage)
    app/agents/x/y.py      -> app.agents.x.y       -> 'agents'
    """
    # mod_parts excludes the leading 'app'
    if not mod_parts:
        return "app"
    return mod_parts[0]


def _module_parts(py_file: Path) -> list[str]:
    """['graph', 'driver'] for backend/app/graph/driver.py (drops 'app')."""
    rel = py_file.relative_to(BACKEND_APP)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _is_submodule(parts: list[str]) -> bool:
    """True if backend/app/<parts> is a real module file or package dir."""
    base = BACKEND_APP.joinpath(*parts)
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def _resolve_import(node: ast.ImportFrom | ast.Import, src_parts: list[str]):
    """Yield resolved 'app.*' dotted targets (relative to app) for one import.

    src_parts is the source module's parts (without 'app'). Handles absolute
    `app.x` imports and relative `.`/`..` imports resolved against the source
    package. For `from app.pkg import name`, the imported NAME is resolved
    against the filesystem: if `app.pkg.name` is itself a module the edge points
    at it (so `from app import budget` counts as an edge to `budget`, not the
    package root). Only intra-app targets are yielded.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            dotted = alias.name.split(".")
            if dotted and dotted[0] == "app":
                yield dotted[1:]
        return

    # ImportFrom
    if node.level and node.level > 0:
        # Relative import. The source package is src_parts minus the module file
        # itself; each dot beyond the first climbs one more package level.
        pkg = list(src_parts[:-1]) if src_parts else []
        climb = node.level - 1
        if climb > 0:
            pkg = pkg[: max(0, len(pkg) - climb)]
        mod_parts = pkg + (node.module.split(".") if node.module else [])
    elif node.module and node.module.split(".")[0] == "app":
        mod_parts = node.module.split(".")[1:]
    else:
        return

    # Prefer the imported names when they are real submodules of mod_parts.
    emitted = False
    for alias in node.names:
        candidate = mod_parts + [alias.name]
        if _is_submodule(candidate):
            yield candidate
            emitted = True
    if not emitted:
        yield mod_parts


def section_modularity(out: Tee) -> None:
    _section(out, "4. Modularity — module-level import graph (inferential)")

    # (src_bucket, dst_bucket) -> {'top': n, 'lazy': n}
    edges: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"top": 0, "lazy": 0})
    buckets: set[str] = set()

    for py_file in sorted(BACKEND_APP.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        src_parts = _module_parts(py_file)
        src_bucket = _bucket_for_module(src_parts)
        buckets.add(src_bucket)
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        # Mark which import nodes are lazy (nested inside a function/method).
        lazy_ids: set[int] = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(fn):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        lazy_ids.add(id(inner))

        for imp in ast.walk(tree):
            if not isinstance(imp, (ast.Import, ast.ImportFrom)):
                continue
            for target in _resolve_import(imp, src_parts):
                dst_bucket = _bucket_for_module(target)
                buckets.add(dst_bucket)
                if dst_bucket == src_bucket:
                    continue  # intra-bucket coupling isn't a boundary signal
                kind = "lazy" if id(imp) in lazy_ids else "top"
                edges[(src_bucket, dst_bucket)][kind] += 1

    out.write(f"\nbuckets ({len(buckets)}): {', '.join(sorted(buckets))}\n")
    out.write(f"cross-bucket edges: {len(edges)}\n\n")
    out.write("edges (src -> dst : top-level / lazy):\n")
    for (src, dst) in sorted(edges):
        c = edges[(src, dst)]
        out.write(f"  {src:>12} -> {dst:<12}  top={c['top']:<3} lazy={c['lazy']}\n")

    # Fan-in / fan-out summary, a quick read on hub modules.
    fan_out: dict[str, int] = defaultdict(int)
    fan_in: dict[str, int] = defaultdict(int)
    for (src, dst) in edges:
        fan_out[src] += 1
        fan_in[dst] += 1
    out.write("\nfan-out (buckets a module imports from):\n")
    for b in sorted(buckets, key=lambda x: (-fan_out[x], x)):
        if fan_out[b]:
            out.write(f"  {b:>12}  -> {fan_out[b]}\n")
    out.write("\nfan-in (buckets that import a module):\n")
    for b in sorted(buckets, key=lambda x: (-fan_in[x], x)):
        if fan_in[b]:
            out.write(f"  {b:>12}  <- {fan_in[b]}\n")

    # Paste-ready prompt — the inferential pass runs OUTSIDE make drift.
    out.write("\n" + "-" * 72 + "\n")
    out.write("LLM REVIEW PROMPT (paste to an assistant — make drift never calls an LLM):\n")
    out.write("-" * 72 + "\n")
    out.write(
        "You are reviewing the module boundaries of a FastAPI + ADK backend\n"
        "(package `app`). Below is the module-level import graph between top-level\n"
        "buckets, with counts of top-level vs lazy (function-local) imports.\n\n"
        "Ground every observation in an edge below — do not speculate about code\n"
        "you cannot see. Flag, in priority order:\n"
        "  1. Layering violations (e.g. low-level modules importing agents/api).\n"
        "  2. Cycles between buckets (A->B and B->A).\n"
        "  3. Edges that exist ONLY as lazy imports — often a smell hiding a cycle\n"
        "     or a heavy dependency that belongs behind an interface.\n"
        "  4. Hub buckets (high fan-in AND high fan-out) that may need splitting.\n"
        "For each finding: name the edge(s), say why it's a concern, and suggest\n"
        "the smallest change. If the graph looks healthy, say so plainly.\n\n"
        "Import graph:\n"
    )
    for (src, dst) in sorted(edges):
        c = edges[(src, dst)]
        out.write(f"  {src} -> {dst} (top={c['top']}, lazy={c['lazy']})\n")


def _selftest() -> int:
    """Unit-check the pure AST helpers (no tools, git, or network needed)."""
    failures: list[str] = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # bucketing: top-level module vs subpackage vs deep path
    check("bucket top module", _bucket_for_module(["auth"]), "auth")
    check("bucket subpackage", _bucket_for_module(["graph", "driver"]), "graph")
    check("bucket deep", _bucket_for_module(["agents", "people", "research"]), "agents")
    check("bucket root", _bucket_for_module([]), "app")

    def resolve(src: str, code: str):
        """(src module parts, source) -> sorted list of (bucket, is_lazy)."""
        tree = ast.parse(code)
        src_parts = src.split(".") if src else []
        lazy_ids: set[int] = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(fn):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        lazy_ids.add(id(inner))
        results = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in _resolve_import(node, src_parts):
                    results.append((_bucket_for_module(target), id(node) in lazy_ids))
        return sorted(results)

    # absolute dotted import -> bucket from the second component
    check("abs from", resolve("api.routes", "from app.graph.driver import x"),
          [("graph", False)])
    check("abs import", resolve("main", "import app.tools.web"),
          [("tools", False)])
    # non-app imports are ignored
    check("stdlib ignored", resolve("main", "import os\nfrom typing import List"), [])
    # `from app import <submodule>` resolves to the submodule's own bucket
    # (budget.py + ratelimit.py exist under app/), not the package root.
    check("from app submodule", resolve("tools.web", "from app import budget, ratelimit"),
          [("budget", False), ("ratelimit", False)])
    # lazy (function-local) import is flagged
    check("lazy import",
          resolve("graph.jobs", "def f():\n    from app.agents.x import y\n"),
          [("agents", True)])
    # relative import resolves against the source package (graph.jobs -> graph.driver)
    check("relative sibling",
          resolve("graph.jobs", "from .driver import get_driver"),
          [("graph", False)])

    if failures:
        print("DRIFT SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("drift selftest OK (pure AST helpers)")
    return 0


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    out = Tee()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.write(f"Nebula drift report — {now}\n")
    out.write("Read-only, advisory (not a gate). Every 3rd wave (#108).\n")

    section_dead_code(out)
    section_dep_freshness(out)
    section_secrets(out)
    section_modularity(out)

    out.write("\n" + "=" * 72 + "\n")
    out.write("end of drift report\n")

    try:
        REPORT_PATH.write_text(out.getvalue(), encoding="utf-8")
        print(f"\n(written to {REPORT_PATH.relative_to(REPO_ROOT)})")
    except OSError as exc:  # pragma: no cover — best effort
        print(f"\n(could not write report file: {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

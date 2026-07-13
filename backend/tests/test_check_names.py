"""Pytest bridge for the tracked-name leak sensor's pure logic (#104).

The deterministic core (normalisation matching, redaction, diff parsing, fuzzy
gating) lives in `scripts/check_names.py` and self-tests via `--selftest` with
FICTIONAL names only — the repo is public, so no real tracked name may appear in
any test. Running that selftest here keeps `make test` the single green gate,
and a few direct calls pin the leak-critical behaviours (redaction never echoes
a name; generic words don't false-positive; distinctive names do fire).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_names.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_names", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_selftest_passes():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_redaction_never_leaks_name():
    m = _load()
    red = m.redact_line("we onboarded Globex Corp last week", "Globex Corp")
    assert "globex" not in red.lower()
    assert red.startswith("we onboarded G")


def test_generic_word_not_flagged():
    m = _load()
    matches = m.find_matches(
        ["Systems", "Data"], [("f.py", 1, "distributed systems process the data")]
    )
    assert matches == []


def test_distinctive_name_flagged_with_line_number():
    m = _load()
    matches = m.find_matches(["Globex"], [("f.py", 3, "call the Globex API")])
    assert len(matches) == 1
    assert matches[0]["strength"] == "exact"
    assert matches[0]["lineno"] == 3


def test_containment_gate_needs_distinctive_token():
    m = _load()
    # 'Initech Labs' -> distinctive 'initech' + generic 'labs'; non-contiguous
    # subset present (contiguous would be an exact hit).
    hit = m.find_matches(["Initech Labs"], [("x", 2, "Initech opened a new Labs site")])
    assert hit and hit[0]["strength"] == "containment"

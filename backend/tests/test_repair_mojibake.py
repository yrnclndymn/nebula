"""Repair pass for already-stored mojibake Signal text (#89).

Pure tests cover the conservative detector (`demojibake`) and the write plan: it
must repair a reversibly-corrupted string, leave clean text (including text with
GENUINE curly quotes) untouched, and never touch a string that doesn't carry the
mojibake signature. The graph test seeds a mojibake Signal and checks the repair
is idempotent; it skips cleanly when Neo4j is unreachable (CI is the arbiter).

All fixtures are fictional (Acme/Globex).
"""

import asyncio

import pytest

from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.repair_mojibake import _plan_repairs, demojibake, repair_mojibake
from app.graph.schema import apply_schema

# A clean string and its mojibake twin (UTF-8 bytes wrongly shown as ISO-8859-1).
_CLEAN = "Acme’s café raises €5m… “more”"
_MOJIBAKE = _CLEAN.encode("utf-8").decode("latin-1")


def test_mojibake_roundtrips_back_to_clean():
    assert _MOJIBAKE != _CLEAN
    assert demojibake(_MOJIBAKE) == _CLEAN


def test_clean_text_is_left_alone():
    # Plain ASCII, and text with REAL curly quotes, must never be "repaired".
    assert demojibake("Acme raises Series B") is None
    assert demojibake(_CLEAN) is None  # already correct (has real U+2019 etc.)
    assert demojibake("") is None
    assert demojibake(None) is None


def test_only_repairs_when_marker_present():
    # A pure-Latin-1 string with no mojibake marker is not touched, even though it
    # could technically be latin-1-encoded.
    assert demojibake("Cafe resume") is None


def test_does_not_double_repair():
    # Running the detector on already-repaired text is a no-op (idempotent).
    assert demojibake(demojibake(_MOJIBAKE)) is None


def test_plan_repairs_covers_title_and_summary():
    rows = [
        {"eid": "1", "title": _MOJIBAKE, "summary": None},
        {"eid": "2", "title": "Clean title", "summary": _MOJIBAKE},
        {"eid": "3", "title": "Clean", "summary": "Also clean"},
    ]
    plan = _plan_repairs(rows)
    by_eid = {item["eid"]: item for item in plan}
    assert set(by_eid) == {"1", "2"}  # node 3 untouched
    assert by_eid["1"]["changes"]["title"]["after"] == _CLEAN
    assert "summary" not in by_eid["1"]["changes"]
    assert by_eid["2"]["changes"]["summary"]["after"] == _CLEAN


# --- Graph integration: seed mojibake, repair, assert idempotent ---------------

MARK = "__pytest_repair89__"


async def _neo4j_available() -> bool:
    try:
        await check_connectivity()
        return True
    except Exception:
        return False


async def _cleanup(driver) -> None:
    async with driver.session() as session:
        await session.run(f"MATCH (s:Signal) WHERE s.url CONTAINS '{MARK}' DETACH DELETE s")


def test_repair_mojibake_graph_dry_run_then_commit():
    async def scenario():
        if not await _neo4j_available():
            return "skip"
        driver = get_driver()
        await apply_schema(driver)
        await _cleanup(driver)
        url = f"https://acme.example/{MARK}/1"
        clean_url = f"https://acme.example/{MARK}/2"
        async with driver.session() as session:
            await session.run(
                "CREATE (:Signal {url:$u, title:$t, summary:$s})",
                u=url,
                t=_MOJIBAKE,
                s=_MOJIBAKE,
            )
            # A clean signal that must be left exactly as-is.
            await session.run("CREATE (:Signal {url:$u, title:$t})", u=clean_url, t="Acme ships v2")

        dry = await repair_mojibake(driver, dry_run=True)
        async with driver.session() as session:
            res = await session.run("MATCH (s:Signal {url:$u}) RETURN s.title AS t", u=url)
            still_broken = (await res.single())["t"]

        committed = await repair_mojibake(driver, dry_run=False)
        async with driver.session() as session:
            res = await session.run(
                "MATCH (s:Signal {url:$u}) RETURN s.title AS t, s.summary AS s", u=url
            )
            fixed = dict(await res.single())

        rerun = await repair_mojibake(driver, dry_run=False)
        await _cleanup(driver)
        await close_driver()
        return dry, still_broken, committed, fixed, rerun

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-ephemeral`")
    dry, still_broken, committed, fixed, rerun = out
    assert len(dry["repairs"]) == 1 and dry["dry_run"] is True
    assert still_broken == _MOJIBAKE  # dry run wrote nothing
    assert len(committed["repairs"]) == 1
    assert fixed["t"] == _CLEAN and fixed["s"] == _CLEAN  # both fields repaired
    assert rerun["repairs"] == []  # idempotent: nothing left to fix

"""Repair mojibake in already-stored Signal text (#89).

Signals captured before the decoding fix stored mangled titles/summaries — UTF-8
bytes that were decoded as ISO-8859-1, so ``’`` reads as ``â€™`` and ``…`` as
``â€¦``. That corruption is deterministic and reversible: re-encoding the mangled
string back to ISO-8859-1 bytes and decoding those as UTF-8 recovers the original
(the same "deterministic, reversible" class as the #39 same-URL Person merge).

`demojibake` is the conservative, pure detector. It only ever returns a repair when
the string (a) carries a mojibake marker, (b) is entirely Latin-1 encodable — a
string with a *genuine* ``’`` (U+2019) can't be, so real text is refused — and
(c) round-trips to a DIFFERENT, valid UTF-8 string. Clean text is never touched and
re-running the repaired text is a no-op, so the pass is idempotent.

`repair_mojibake` applies it to Signal ``title``/``summary`` and, like the #39
migration, is dry-run by default and reports every change:

    cd backend && uv run python -m app.graph.repair_mojibake [--commit]
    # or:  make repair-mojibake ARGS=--commit
"""

import asyncio

from neo4j import AsyncDriver

# Byte sequences that only appear when UTF-8 multibyte characters were decoded as
# ISO-8859-1: ``â€…`` fronts the curly quotes / dashes / ellipsis, ``Ã…``/``Â…`` the
# accented Latin letters and symbols. A clean English string won't contain these.
# Visible CP1252-style artefacts…
_MOJIBAKE_MARKERS = ("â€", "Ã", "Â")
# …and the form requests' true ISO-8859-1 decode actually produces: UTF-8
# continuation bytes 0x80-0x9F land on C1 CONTROL characters (invisible), so
# "\u2019" becomes "â" + U+0080 + U+0099 with no visible marker at all. C1
# controls never occur in legitimate text — their presence is the smoking gun.


def _has_c1_controls(text: str) -> bool:
    return any("\x80" <= ch <= "\x9f" for ch in text)


# The fields on a :Signal that hold human-readable captured text.
_TEXT_FIELDS = ("title", "summary")


def demojibake(text: str | None) -> str | None:
    """The recovered UTF-8 string if ``text`` is reversible mojibake, else None.

    Conservative by construction: returns None unless the string carries a mojibake
    marker AND cleanly round-trips (latin-1 encode → utf-8 decode) to a different,
    valid string. Genuine text — including text with real curly quotes — fails the
    latin-1 encode and is left untouched. Idempotent: feeding it repaired text
    yields None.
    """
    if not text:
        return None
    if not (_has_c1_controls(text) or any(marker in text for marker in _MOJIBAKE_MARKERS)):
        return None
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None  # real non-Latin-1 chars, or not valid UTF-8 underneath
    if repaired == text:
        return None
    return repaired


def _plan_repairs(rows: list[dict]) -> list[dict]:
    """Pure: from Signal rows (``eid`` + text fields), the list of nodes needing a
    repair. Each entry is ``{eid, changes:{field:{before, after}}}`` — only fields
    that actually change are listed, and a node with no changes is omitted."""
    plan: list[dict] = []
    for row in rows:
        changes: dict[str, dict[str, str]] = {}
        for field in _TEXT_FIELDS:
            fixed = demojibake(row.get(field))
            if fixed is not None:
                changes[field] = {"before": row[field], "after": fixed}
        if changes:
            plan.append({"eid": row["eid"], "changes": changes})
    return plan


async def _signal_rows(driver: AsyncDriver) -> list[dict]:
    """Every Signal that has some text, keyed by its stable elementId."""
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (s:Signal)
            WHERE s.title IS NOT NULL OR s.summary IS NOT NULL
            RETURN elementId(s) AS eid, s.title AS title, s.summary AS summary
            """
        )
        return [dict(record) async for record in result]


async def repair_mojibake(driver: AsyncDriver, *, dry_run: bool = True) -> dict:
    """Find and (unless ``dry_run``) repair mojibake in Signal titles/summaries.

    Idempotent — a clean graph, or a second run, reports zero repairs. Only the
    changed properties are written (``SET s += props``), so nothing else on the node
    is disturbed. Returns the full plan for the operator to review.
    """
    rows = await _signal_rows(driver)
    plan = _plan_repairs(rows)

    if not dry_run:
        async with driver.session() as session:
            for item in plan:
                props = {field: change["after"] for field, change in item["changes"].items()}
                await session.run(
                    "MATCH (s:Signal) WHERE elementId(s) = $eid SET s += $props",
                    eid=item["eid"],
                    props=props,
                )

    return {"dry_run": dry_run, "signals_scanned": len(rows), "repairs": plan}


def _print_report(report: dict) -> None:
    verb = "would repair" if report["dry_run"] else "repaired"
    for item in report["repairs"]:
        for field, change in item["changes"].items():
            print(f"  {verb} {field}: {change['before']!r} -> {change['after']!r}")
    print(
        f"\n{len(report['repairs'])} signal(s) {'to repair' if report['dry_run'] else 'repaired'} "
        f"across {report['signals_scanned']} signal(s) with text."
        + ("" if not report["dry_run"] else "  Re-run with --commit to apply.")
    )


async def _main() -> None:
    import argparse

    from app.graph.driver import close_driver, get_driver

    parser = argparse.ArgumentParser(description="Repair mojibake in stored Signal text (#89).")
    parser.add_argument(
        "--commit", action="store_true", help="apply the repairs (default: dry-run report)"
    )
    args = parser.parse_args()

    driver = get_driver()
    try:
        report = await repair_mojibake(driver, dry_run=not args.commit)
        _print_report(report)
    finally:
        await close_driver()


if __name__ == "__main__":
    asyncio.run(_main())

"""Import a CSV export of the research sheet into the graph.

    make import CSV=data/companies.csv TOPIC="SAP ecosystem"

Deterministic columns map straight to :Company properties; the freeform columns
(Notes, Leadership, Partnerships, Clients) go through the LLM extractor. Every
write is an idempotent upsert, so re-importing an updated CSV is safe.

Since the sheet has no topic column, pass --topic to tag every row in the file
(keep one domain per CSV, or run twice on split files).
"""

import argparse
import asyncio
import csv
import re
from pathlib import Path

from app.graph.driver import close_driver, get_driver
from app.graph.models import CompanyRecord
from app.graph.repository import upsert_company
from app.importer.extract import ExtractedFields, extract_fields, new_client

# normalized sheet header -> internal field name
HEADER_MAP = {
    "company": "name",
    "priority": "priority",
    "about": "about",
    "source": "source",
    "website": "website",
    "linkedin": "linkedin",
    "hqlocation": "hq_location",
    "headcount": "headcount",
    "estimatedrevenues": "estimated_revenue",
    "estimatedrevenue": "estimated_revenue",
    "partnerships": "partnerships_raw",
    "clients": "clients_raw",
    "notes": "notes_raw",
    "leadership": "leadership_raw",
}

_SPLIT = re.compile(r"[,\n;]")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _int_or_none(value: str) -> int | None:
    match = re.search(r"\d[\d,]*", value or "")
    return int(match.group().replace(",", "")) if match else None


def _map_row(raw: dict[str, str]) -> dict[str, str]:
    """Re-key a CSV row by internal field name, tolerating header variations."""
    out: dict[str, str] = {}
    for header, cell in raw.items():
        field = HEADER_MAP.get(_norm(header or ""))
        if field:
            out[field] = (cell or "").strip()
    return out


def heuristic_extract(row: dict[str, str]) -> ExtractedFields:
    """Cheap, no-LLM fallback (--no-llm): delimiter-split lists, notes as-is."""
    return ExtractedFields(
        notes=row.get("notes_raw") or None,
        partnerships=[
            p.strip() for p in _SPLIT.split(row.get("partnerships_raw", "")) if p.strip()
        ],
        clients=[c.strip() for c in _SPLIT.split(row.get("clients_raw", "")) if c.strip()],
    )


def build_record(
    row: dict[str, str], topic: str | None, ex: ExtractedFields
) -> CompanyRecord | None:
    name = row.get("name", "").strip()
    if not name:
        return None

    return CompanyRecord(
        name=name,
        priority=row.get("priority") or None,
        about=row.get("about") or None,
        source=row.get("source") or None,
        website=row.get("website") or None,
        linkedin=row.get("linkedin") or None,
        hq_location=row.get("hq_location") or None,
        headcount=_int_or_none(row.get("headcount", "")),
        estimated_revenue=row.get("estimated_revenue") or None,
        year_founded=ex.year_founded,
        funding=ex.funding,
        company_types=ex.company_types,
        notes=ex.notes,
        leadership=ex.leadership,
        partnerships=ex.partnerships,
        clients=ex.clients,
        topics=[topic] if topic else [],
    )


async def _already_imported(driver, topic: str | None) -> set[str]:
    """Names of companies already tagged with this topic (i.e. fully imported)."""
    if not topic:
        return set()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Company)-[:TAGGED_AS]->(:Topic {name: $topic}) RETURN c.name AS name",
            topic=topic,
        )
        return {record["name"] async for record in result}


async def run(
    path: Path,
    topic: str | None,
    limit: int | None,
    dry_run: bool,
    use_llm: bool,
    skip_existing: bool = False,
) -> None:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [_map_row(r) for r in csv.DictReader(fh)]
    if limit:
        rows = rows[:limit]

    driver = None if dry_run else get_driver()
    client = new_client() if use_llm else None
    # On a retry run, skip rows already imported so we only spend LLM calls on gaps.
    done = await _already_imported(driver, topic) if (skip_existing and driver) else set()
    written = skipped = 0
    failed: list[str] = []
    for i, row in enumerate(rows, 1):
        name = row.get("name", "").strip()
        if not name:
            skipped += 1
            continue
        if name in done:
            skipped += 1
            continue
        # Isolate each row: a row that fails extraction/upsert (e.g. Gemini still
        # 5xx after retries) is logged and skipped, never sinking the whole batch.
        # Re-running is idempotent, so a later pass fills any gaps in place.
        try:
            if use_llm:
                ex = await extract_fields(
                    company=name,
                    notes=row.get("notes_raw", ""),
                    leadership=row.get("leadership_raw", ""),
                    partnerships=row.get("partnerships_raw", ""),
                    clients=row.get("clients_raw", ""),
                    client=client,
                )
            else:
                ex = heuristic_extract(row)
            record = build_record(row, topic, ex)
            if record is None:
                skipped += 1
                continue
            if dry_run:
                print(f"[{i}/{len(rows)}] {record.model_dump_json(indent=2, exclude_none=True)}")
            else:
                await upsert_company(driver, record)
                print(f"[{i}/{len(rows)}] upserted {record.name}")
            written += 1
        except Exception as exc:  # noqa: BLE001 — keep going, report at the end
            failed.append(name)
            print(f"[{i}/{len(rows)}] FAILED {name}: {exc}")

    if driver is not None:
        await close_driver()
    verb = "previewed" if dry_run else "upserted"
    print(f"\nDone. {written} companies {verb}, {skipped} skipped, {len(failed)} failed.")
    if failed:
        print("Failed (re-run to retry): " + ", ".join(failed))


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the research sheet CSV into Neo4j.")
    parser.add_argument("csv", type=Path, help="Path to the exported CSV")
    parser.add_argument("--topic", help="Tag every row with this research topic")
    parser.add_argument("--limit", type=int, help="Only process the first N rows")
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse + extract, print, don't write"
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="Skip LLM extraction (heuristic only)"
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip rows already imported for this topic (retry runs)",
    )
    args = parser.parse_args()

    if not args.csv.exists():
        parser.error(f"CSV not found: {args.csv}")

    asyncio.run(
        run(
            args.csv,
            args.topic,
            args.limit,
            args.dry_run,
            not args.no_llm,
            args.skip_existing,
        )
    )


if __name__ == "__main__":
    main()

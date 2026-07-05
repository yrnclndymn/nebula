"""Graph-write tool for the enrichment agent — its one side-effecting tool.

The agent gathers facts with the web tools, then calls `save_company` once. All
params are required (no defaults) so ADK generates a clean function schema; the
agent passes "" / 0 / [] for anything it didn't find. This reuses the same
`CompanyRecord` → `upsert_company` write path as the CSV importer, so an agent
enrichment and a sheet import land identically in the graph.
"""

from app.graph.driver import get_driver
from app.graph.models import CompanyRecord, Leader
from app.graph.repository import upsert_company
from app.importer.extract import canonical_company_types


def _parse_leaders(leadership: list[str]) -> list[Leader]:
    leaders: list[Leader] = []
    for item in leadership:
        name, _, title = item.partition("|")
        name = name.strip()
        if name:
            leaders.append(Leader(name=name, title=title.strip() or None))
    return leaders


async def save_company(
    name: str,
    topic: str,
    about: str,
    website: str,
    hq_location: str,
    headcount: int,
    estimated_revenue: str,
    year_founded: int,
    funding: str,
    notes: str,
    company_types: list[str],
    partnerships: list[str],
    clients: list[str],
    leadership: list[str],
) -> dict:
    """Save or update a researched company in the Nebula graph.

    Call this EXACTLY ONCE, after gathering facts. Pass "" for unknown text, 0 for
    unknown numbers, and [] for unknown lists — never invent values. Format each
    leadership entry as "Name | Title" (title may be empty). company_types should
    only be ownership/structure labels like "B-Corp", "ESOP", "employee-owned".
    """
    record = CompanyRecord(
        name=name,
        about=about or None,
        website=website or None,
        hq_location=hq_location or None,
        headcount=headcount or None,
        estimated_revenue=estimated_revenue or None,
        year_founded=year_founded or None,
        funding=funding or None,
        notes=notes or None,
        topics=[topic] if topic else [],
        company_types=canonical_company_types(company_types),
        partnerships=partnerships,
        clients=clients,
        leadership=_parse_leaders(leadership),
    )
    await upsert_company(get_driver(), record)
    return {
        "saved": record.name,
        "scalar_fields_set": len(record.scalar_props()),
        "partnerships": len(record.partnerships),
        "clients": len(record.clients),
        "leaders": len(record.leadership),
        "company_types": record.company_types,
    }

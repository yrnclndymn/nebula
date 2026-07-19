"""Graph-write tool for the enrichment agent — its one side-effecting tool.

The agent gathers facts with the web tools, then calls `save_company` once. All
params are required (no defaults) so ADK generates a clean function schema; the
agent passes "" / 0 / [] for anything it didn't find. This reuses the same
`CompanyRecord` → `upsert_company` write path as the CSV importer, so an agent
enrichment and a sheet import land identically in the graph.
"""

from contextvars import ContextVar

from app.graph.cache import domain_of
from app.graph.company_types import canonical_company_types
from app.graph.driver import get_driver
from app.graph.models import Citation, CompanyRecord, Leader
from app.graph.repository import upsert_company
from app.tools.social import normalize_linkedin

# When set to a list (by the propose flow), save_company appends the record it
# WOULD write and does not touch the graph — enabling propose→review→commit.
# Default None means normal behaviour: write immediately.
proposal_sink: ContextVar[list | None] = ContextVar("nebula_proposal_sink", default=None)


def _parse_leaders(leadership: list[str]) -> list[Leader]:
    leaders: list[Leader] = []
    for item in leadership:
        name, _, title = item.partition("|")
        name = name.strip()
        if name:
            leaders.append(Leader(name=name, title=title.strip() or None))
    return leaders


# Fields that must not be saved without a citation, and the citation-field
# labels that count as citing them (the model isn't always exact).
_MUST_CITE = {
    "funding": {"funding"},
    "estimated_revenue": {"estimated_revenue", "estimatedrevenue", "revenue", "est_revenue"},
    "headcount": {"headcount", "headcounts", "employees", "employee_count", "staff"},
}


def _drop_uncited(values: dict, citations: list[Citation]) -> tuple[dict, list[str]]:
    """Enforce "no number without a source": null out any must-cite field whose
    value isn't backed by a matching citation. Returns (kept_values, dropped)."""
    cited = {c.field.strip().lower() for c in citations}
    kept = dict(values)
    dropped: list[str] = []
    for field, aliases in _MUST_CITE.items():
        if kept.get(field) and not (cited & aliases):
            kept[field] = None
            dropped.append(field)
    return kept, dropped


def _is_linkedin_citation(c: Citation) -> bool:
    return "linkedin" in c.field.strip().lower()


def _linkedin_has_own_site_provenance(website: str, citations: list[Citation]) -> bool:
    """Is the LinkedIn deterministically attributable to the company's OWN site?

    True only when some LinkedIn citation's source URL sits on the company's own
    website domain — the enrichment mirror of the back-fill's own-site social scrape.
    Crawled/searched content is untrusted, so a LinkedIn cited only to a search result
    or partner page (or cited nowhere) is NOT its own-site link and stays out of the
    canonical field.
    """
    if not website:
        return False
    site = domain_of(website)
    if not site:
        return False
    return any(
        _is_linkedin_citation(c) and c.source and domain_of(c.source) == site for c in citations
    )


def _linkedin_candidate_citation(linkedin: str, citations: list[Citation]) -> list[Citation]:
    """Surface a non-own-site LinkedIn as a review candidate rather than dropping it:
    keep whatever citation the agent gave for it, else add a citation-only entry so the
    reviewer still sees the candidate (as a CITES edge) without it becoming canonical."""
    if any(_is_linkedin_citation(c) for c in citations):
        return citations
    return [*citations, Citation(field="linkedin", value=linkedin, source=linkedin)]


def _parse_citations(citations: list[str]) -> list[Citation]:
    """Each item is "field | value | source_url | source_date" (date optional).

    Locates the URL by content, not slot, so an extra pipe in the value doesn't
    misalign the source/date (the model occasionally adds one).
    """
    parsed: list[Citation] = []
    for item in citations:
        parts = [p.strip() for p in item.split("|")]
        if len(parts) < 3 or not parts[0]:
            continue
        url = next((p for p in parts[2:] if p.startswith("http")), None)
        if not url:
            continue
        date = next((p for p in parts[2:] if p != url and not p.startswith("http")), None)
        parsed.append(Citation(field=parts[0], value=parts[1], source=url, source_date=date))
    return parsed


async def save_company(  # noqa: PLR0913 — ADK builds the tool schema from these params; one flat field per company attribute is the interface, not an over-long signature to split
    name: str,
    topic: str,
    about: str,
    website: str,
    linkedin: str,
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
    citations: list[str],
) -> dict:
    """Save or update a researched company in the Nebula graph.

    Call this EXACTLY ONCE, after gathering facts. Pass "" for unknown text, 0 for
    unknown numbers, and [] for unknown lists — never invent values. Format each
    leadership entry as "Name | Title" (title may be empty). company_types should
    only be ownership/structure labels like "B-Corp", "ESOP", "employee-owned".

    linkedin: the company's LinkedIn URL. It is saved as the company's canonical
    LinkedIn ONLY when you cite it to a page on the company's OWN website domain (the
    social link the company itself publishes) — pass a citation
    "linkedin | <url> | <own-site page url> | <date>". A LinkedIn cited only to a
    search result or off-site page (untrusted content) is not saved as the canonical
    field; it is kept as a candidate for the user to review. Absent LinkedIn: pass "".

    citations: provenance for the facts you save — one entry per checkable fact,
    formatted "field | value | source_url | source_date". `field` matches a saved
    field (e.g. funding, headcount, year_founded, hq_location, linkedin); `source_url` is the
    page you got it from; `source_date` is when the info is from (e.g. "2025-09" or
    "as of 2024"). REQUIRED for every financial figure and headcount you save, and for
    a LinkedIn to be saved as the canonical field (cite it to the company's own site).
    """
    parsed_citations = _parse_citations(citations)
    # Guardrail (#21): the LinkedIn becomes the canonical field ONLY with deterministic
    # own-site provenance — a citation whose source is on the company's own domain, the
    # enrichment mirror of the back-fill's own-site social scrape. A LinkedIn from
    # crawled/searched content (untrusted input) is not silently dropped but demoted to
    # a citation-only candidate the reviewer confirms — it must never steer the write.
    linkedin_norm = normalize_linkedin(linkedin) if linkedin else None
    linkedin_own_site = bool(linkedin_norm) and _linkedin_has_own_site_provenance(
        website, parsed_citations
    )
    if linkedin_norm and not linkedin_own_site:
        parsed_citations = _linkedin_candidate_citation(linkedin_norm, parsed_citations)
    # Guardrail: financials/headcount are dropped unless a citation backs them.
    guarded, dropped = _drop_uncited(
        {
            "funding": funding or None,
            "estimated_revenue": estimated_revenue or None,
            "headcount": headcount or None,
        },
        parsed_citations,
    )
    record = CompanyRecord(
        name=name,
        about=about or None,
        website=website or None,
        linkedin=linkedin_norm if linkedin_own_site else None,
        hq_location=hq_location or None,
        headcount=guarded["headcount"],
        estimated_revenue=guarded["estimated_revenue"],
        year_founded=year_founded or None,
        funding=guarded["funding"],
        notes=notes or None,
        topics=[topic] if topic else [],
        company_types=canonical_company_types(company_types),
        partnerships=partnerships,
        clients=clients,
        leadership=_parse_leaders(leadership),
        citations=parsed_citations,
        origin="agent",
    )
    result = {
        "saved": record.name,
        "scalar_fields_set": len(record.scalar_props()),
        "partnerships": len(record.partnerships),
        "clients": len(record.clients),
        "leaders": len(record.leadership),
        "citations": len(record.citations),
        "dropped_uncited": dropped,
        "linkedin_candidate_only": bool(linkedin_norm) and not linkedin_own_site,
        "company_types": record.company_types,
    }

    sink = proposal_sink.get()
    if sink is not None:
        # Propose mode: capture the record for review, do not write. Frame it as
        # success so the agent doesn't retry save_company (it reads "written").
        sink[:] = [record.model_dump()]  # keep only the latest if it does re-call
        return {
            **result,
            "written": True,
            "note": "Recorded for the user to review. Done — do NOT call save_company again.",
        }

    await upsert_company(get_driver(), record)
    return {**result, "written": True}

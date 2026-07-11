"""Domain models — the structured shape of a company record.

Both the Google Sheet importer and the enrichment agents produce a
`CompanyRecord`; `repository.upsert_company` writes it into the graph. Keeping
this one shape between "data in" and "graph write" is what keeps the pipeline
deterministic.
"""

import re

from pydantic import BaseModel, Field

# What kind of business a company is (distinct from ownership CompanyType).
# The first three are *ecosystem* players — companies worth researching. "client"
# marks an end-customer organisation (a bank/retailer/public-sector body pulled in
# via HAS_CLIENT) that is not an ecosystem player and is not a research target.
ECOSYSTEM_KINDS = ("service_provider", "isv", "cloud_provider")
KINDS = ECOSYSTEM_KINDS + ("client",)
# A custom field applies to one kind, or to every company. DELIBERATE DECISION:
# custom research fields are ecosystem-only — a field can target an ecosystem kind
# (or "all") but NOT "client". Custom fields capture things we research about
# ecosystem players (service lines, product tiers, …); end-customer orgs are not
# researched, so letting a field target "client" would only invite dead columns.
# ("all"-scoped fields still nominally cover clients, but clients carry no
# researched data to fill them, so they render as blanks — see fieldApplies.)
APPLIES_TO = ECOSYSTEM_KINDS + ("all",)


def field_key(label: str) -> str:
    """Slug a field label into a graph property key, e.g. 'Service Lines' -> 'serviceLines'."""
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", label) if w]
    if not words:
        return "field"
    return words[0].lower() + "".join(w.capitalize() for w in words[1:])


class FieldDef(BaseModel):
    """A user-defined custom field (e.g. service lines for service providers)."""

    name: str  # property key on Company
    label: str  # display label
    description: str  # what to research (used by the back-fill extractor)
    applies_to_kind: str = "all"  # a kind, or "all"
    type: str = "list"  # "list" | "text"


class Leader(BaseModel):
    name: str
    title: str | None = None


class Citation(BaseModel):
    """Provenance for one fact: which source justifies a value, and its timeliness.

    Stored as (Company)-[:CITES {field, value, sourceDate}]->(Source {url}) so any
    figure — especially financials — can be checked back to its source later.
    """

    field: str  # which CompanyRecord field this justifies, e.g. "funding"
    value: str  # the value as stated by the source
    source: str  # source URL
    source_date: str | None = None  # when the info is from (timeliness), free text


class CompanyRecord(BaseModel):
    # Identity / flat facts (→ :Company properties)
    name: str
    priority: str | None = None
    about: str | None = None
    source: str | None = None
    website: str | None = None
    linkedin: str | None = None
    hq_location: str | None = None
    headcount: int | None = None
    estimated_revenue: str | None = None
    year_founded: int | None = None
    funding: str | None = None  # raw text for now; structured into :INVESTED_IN later
    notes: str | None = None
    origin: str | None = None  # who produced this record: "agent" | "sheet" | "manual"
    kind: str | None = None  # service_provider | isv | cloud_provider | client

    # Tags (→ nodes MERGE'd by name)
    topics: list[str] = Field(default_factory=list)  # research domains
    company_types: list[str] = Field(default_factory=list)  # b-corp, ESOP, …

    # Relationships to other organizations (→ :Company stubs MERGE'd by name)
    partnerships: list[str] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)

    # People
    leadership: list[Leader] = Field(default_factory=list)

    # Provenance — source + timeliness for individual facts (agent-produced).
    citations: list[Citation] = Field(default_factory=list)

    def scalar_props(self) -> dict:
        """Non-null flat properties, keyed as they appear on the graph node."""
        props = {
            "priority": self.priority,
            "about": self.about,
            "source": self.source,
            "website": self.website,
            "linkedin": self.linkedin,
            "hqLocation": self.hq_location,
            "headcount": self.headcount,
            "estimatedRevenue": self.estimated_revenue,
            "yearFounded": self.year_founded,
            "funding": self.funding,
            "notes": self.notes,
            "origin": self.origin,
            "kind": self.kind,
        }
        return {k: v for k, v in props.items() if v is not None}

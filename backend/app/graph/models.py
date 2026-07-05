"""Domain models — the structured shape of a company record.

Both the Google Sheet importer and the enrichment agents produce a
`CompanyRecord`; `repository.upsert_company` writes it into the graph. Keeping
this one shape between "data in" and "graph write" is what keeps the pipeline
deterministic.
"""

from pydantic import BaseModel, Field


class Leader(BaseModel):
    name: str
    title: str | None = None


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

    # Tags (→ nodes MERGE'd by name)
    topics: list[str] = Field(default_factory=list)  # research domains
    company_types: list[str] = Field(default_factory=list)  # b-corp, ESOP, …

    # Relationships to other organizations (→ :Company stubs MERGE'd by name)
    partnerships: list[str] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)

    # People
    leadership: list[Leader] = Field(default_factory=list)

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
        }
        return {k: v for k, v in props.items() if v is not None}

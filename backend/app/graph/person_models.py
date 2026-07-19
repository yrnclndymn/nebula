"""Committable person models — the shape written into the graph (story #40).

These live in the graph layer, mirroring the :class:`app.graph.models.CompanyRecord`
precedent: the *committable* record is what ``app.graph.person_enrichment`` writes, so
it belongs BELOW the research agents rather than above them. The raw, untrusted
research shape (``PersonResearch``) stays in :mod:`app.agents.people.models` and
imports ``PriorRole`` / ``PersonCitation`` from here (a legitimate downward dep);
:func:`app.agents.people.build.build_person_record` derives ``PersonRecord`` from that
research, DETERMINISTICALLY dropping any fact not backed by a citation with a valid
``http(s)`` source URL — so "no fact saved without a citation" holds by construction,
mirroring the company CITES path.

Prior roles are modelled as ``(Person)-[:HELD_ROLE {title, from, to}]->(Company)``
edges rather than a JSON blob on the node: a role names a company (a first-class node
that may itself be tracked/enriched later), so an edge keeps the graph traversable —
e.g. "people who once worked at Acme" — and lets an unknown employer MERGE as a
:Company stub, exactly like partners/clients do.
"""

from pydantic import BaseModel, Field, field_validator

from app.graph.linkedin import canonical_linkedin


class PersonCitation(BaseModel):
    """Provenance for one person fact: which source justifies a value.

    Stored as ``(Person)-[:CITES {field, value, sourceDate}]->(Source {url})`` —
    the same shape company facts use, so a person fact can be checked back to where
    the agent found it.
    """

    field: str  # which fact this justifies, e.g. "bio", "title", "linkedin"
    value: str  # the value as stated by the source
    source: str  # source URL
    source_date: str | None = None  # when the info is from (timeliness), free text


class PriorRole(BaseModel):
    """A past role: a title at a company, over an optional year span. ``source`` is
    the citation URL for the role — a role with no valid source is dropped on build
    (provenance guardrail), so this is effectively required to be committed."""

    company: str
    title: str | None = None
    from_year: int | None = None
    to_year: int | None = None
    source: str | None = None


class PersonRecord(BaseModel):
    """The committable, provenance-filtered person facts written to the graph.

    ``company`` is the *scoping* company — the company this person leads in the
    graph, used to locate their :Person node (never a bare global name-key: the #87
    namesake lesson). ``title`` is their current title, written onto the existing
    ``LEADS`` edge to ``company``.
    """

    name: str
    company: str
    title: str | None = None
    bio: str | None = None
    linkedin: str | None = None  # canonical personal-profile URL (identity, #39)
    personal_site: str | None = None
    talks: list[str] = Field(default_factory=list)
    prior_roles: list[PriorRole] = Field(default_factory=list)
    citations: list[PersonCitation] = Field(default_factory=list)

    @field_validator("linkedin")
    @classmethod
    def _canonicalise_linkedin(cls, v: str | None) -> str | None:
        """Canonicalise the identity URL at the domain boundary (#183): this is the
        single choke point, so ``upsert_person`` receives a canonical-or-None value
        and never has to re-canonicalise (or reach up into ``people`` for it). A
        company/school page or non-profile URL reduces to ``None``. Idempotent."""
        return canonical_linkedin(v)

    def has_facts(self) -> bool:
        """Whether anything survived provenance filtering and is worth committing."""
        return bool(
            self.title
            or self.bio
            or self.linkedin
            or self.personal_site
            or self.talks
            or self.prior_roles
        )

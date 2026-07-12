"""Domain models for person enrichment (story #40, epic #25 People Intelligence).

Two shapes, deliberately separated:

- :class:`PersonResearch` is the *raw, untrusted* structured output of the research
  step (a single Gemini call over crawled/searched evidence). Nothing here is
  trusted — the LLM is asked to cite every fact, but the citation is only a *claim*.
- :class:`PersonRecord` is the *committable* shape: it is derived from a
  ``PersonResearch`` by :func:`app.agents.people.build.build_person_record`, which
  DETERMINISTICALLY drops any fact that is not backed by a citation with a valid
  ``http(s)`` source URL. So the guardrail "no fact saved without a citation" is
  enforced by code, not by trusting the model — mirroring the company CITES path.

Prior roles are modelled as ``(Person)-[:HELD_ROLE {title, from, to}]->(Company)``
edges rather than a JSON blob on the node: a role names a company (a first-class
node that may itself be tracked/enriched later), so an edge keeps the graph
traversable — e.g. "people who once worked at Acme" — and lets an unknown employer
MERGE as a :Company stub, exactly like partners/clients do.
"""

from pydantic import BaseModel, Field

# Scalar fact fields a citation can justify (each needs a matching CITES edge).
# Prior roles carry their own per-role source, so they are handled separately.
PERSON_SCALAR_FIELDS = ("title", "bio", "linkedin", "personal_site", "talks")


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


class PersonResearch(BaseModel):
    """Raw structured research output — UNTRUSTED until reviewed and committed.

    The commit is deterministic because this is a fixed schema; provenance is then
    enforced by :func:`build_person_record`, not by trusting these fields.
    """

    name: str
    current_title: str | None = None
    current_company: str | None = None
    bio: str | None = None
    linkedin: str | None = None
    personal_site: str | None = None
    talks: list[str] = Field(default_factory=list)  # public talk / profile URLs
    prior_roles: list[PriorRole] = Field(default_factory=list)
    citations: list[PersonCitation] = Field(default_factory=list)


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

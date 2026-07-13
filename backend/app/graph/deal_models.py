"""Committable acquisition models — the shape written into the graph (story #43).

These live in the graph layer, mirroring the :class:`app.graph.models.CompanyRecord`
precedent: the *committable* record is what ``app.graph.acquisitions`` writes, so it
belongs BELOW the research agents rather than above them. The raw, untrusted research
shapes (``DealResearch`` / ``AcquisitionResearch``) stay in
:mod:`app.agents.deals.models`; :func:`app.agents.deals.build.build_acquisition_record`
derives these committable shapes from that research, DETERMINISTICALLY dropping any
deal without a valid ``http(s)`` source and dropping the ``amount``/``currency`` of
any deal whose amount is not separately cited — so "no financial figure saved without
a citation" holds by construction.

A deal is modelled as ``(acquirer)-[:ACQUIRED {announcedAt, closedAt, amount,
currency, thesis, source}]->(target)`` — both endpoints are first-class :Company
nodes (MERGE'd as stubs when unknown, which feeds the research backlog), so the graph
stays traversable ("everything Acme has acquired", "who acquired Globex").
"""

from pydantic import BaseModel, Field


class Deal(BaseModel):
    """A committable acquisition: provenance already enforced by the build step.

    ``source`` is required-in-practice (the build drops any deal lacking a valid
    one). ``amount``/``currency`` are present only when ``amount_source`` was a
    valid URL, so a committed amount is always backed by a citation.
    """

    acquirer: str
    target: str
    announced_at: str | None = None
    closed_at: str | None = None
    amount: str | None = None
    currency: str | None = None
    thesis: str | None = None
    source: str
    amount_source: str | None = None


class AcquisitionRecord(BaseModel):
    """The committable, provenance-filtered set of deals written to the graph.

    ``company`` is the subject the research was scoped to (for labelling/review);
    the actual edges are keyed on each deal's ``acquirer``/``target``.
    """

    company: str
    deals: list[Deal] = Field(default_factory=list)

    def has_facts(self) -> bool:
        """Whether any deal survived provenance filtering and is worth committing."""
        return bool(self.deals)

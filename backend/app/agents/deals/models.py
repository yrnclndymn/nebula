"""Raw acquisition-research models (story #43, epic #26 M&A Intelligence).

Two shapes, deliberately separated (the People-Intelligence discipline). This module
holds the *raw, untrusted* research shapes; the *committable* shapes (``Deal`` /
``AcquisitionRecord``) live DOWN in :mod:`app.graph.deal_models` (what the graph
write path consumes), mirroring the ``CompanyRecord`` precedent.

- :class:`AcquisitionResearch` is the raw, untrusted structured output of the
  research step (a single Gemini call over crawled/searched evidence). Every deal
  is asked to cite its facts, but a claimed citation is only a *claim*.
- :class:`app.graph.deal_models.AcquisitionRecord` is the committable shape, derived
  from the research by :func:`app.agents.deals.build.build_acquisition_record`, which
  DETERMINISTICALLY drops any deal without a valid ``http(s)`` source and — the money
  guardrail — drops the ``amount``/``currency`` of any deal whose amount is not
  separately cited. So "no financial figure saved without a citation" holds by
  construction.
"""

from pydantic import BaseModel, Field

# Per-deal citation source fields. ``source`` justifies the deal's existence and
# its non-financial facts (dates, thesis); ``amount_source`` separately justifies
# the amount/currency — a financial figure never rides on the deal citation alone.
DEAL_SOURCE_FIELDS = ("source", "amount_source")


class DealResearch(BaseModel):
    """One acquisition as reported by the (untrusted) research step.

    ``acquirer`` and ``target`` name the two companies; the subject company the
    agent was asked about is one of them (direction is inherent, not a flag).
    ``amount`` is kept as raw text as stated by the source (e.g. ``"$1.2 billion"``)
    — parsing/normalisation is a later concern; provenance is the gate now.
    """

    acquirer: str
    target: str
    announced_at: str | None = None  # raw date/period text as found
    closed_at: str | None = None
    amount: str | None = None  # raw deal value text (financial figure — needs a cite)
    currency: str | None = None  # e.g. "USD"; travels with amount
    thesis: str | None = None  # deal rationale / strategic thesis
    source: str | None = None  # citation URL for the deal's existence + dates/thesis
    amount_source: str | None = None  # citation URL specifically for the amount


class AcquisitionResearch(BaseModel):
    """Raw structured research output — UNTRUSTED until reviewed and committed.

    The commit is deterministic because this is a fixed schema; provenance is then
    enforced by :func:`app.agents.deals.build.build_acquisition_record`, never by
    trusting these fields.
    """

    company: str  # the subject company the research was scoped to
    deals: list[DealResearch] = Field(default_factory=list)

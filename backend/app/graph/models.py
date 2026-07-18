"""Domain models — the structured shape of a company record.

Both the Google Sheet importer and the enrichment agents produce a
`CompanyRecord`; `repository.upsert_company` writes it into the graph. Keeping
this one shape between "data in" and "graph write" is what keeps the pipeline
deterministic.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field, field_validator

from app.graph.linkedin import canonical_linkedin

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
    # Canonical identity where known (story #39). When present the write path keys
    # the :Person on this URL instead of the name. Canonicalised HERE by the
    # validator (#183) — the single choke point, so every producer (importer,
    # enrichment agent) hands the write path a canonical-or-None value and a
    # non-canonical URL can never reach the graph. Populated only via the
    # deterministic own-site / search-evidence discovery path — never a bare
    # crawled link.
    linkedin: str | None = None

    @field_validator("linkedin")
    @classmethod
    def _canonicalise_linkedin(cls, v: str | None) -> str | None:
        """Reduce to the canonical personal-profile key (or ``None`` for a
        company/school page or non-profile URL, which then keys the :Person by
        name). Idempotent, so re-validating an already-canonical value is a no-op."""
        return canonical_linkedin(v)


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


# --- Signals (news / blog / event per company) -----------------------------
# Foundation for the Signals epic: the graph shape every capture agent writes and
# every read serves. Kind is a validated string *property* (not a node label) so
# the vocabulary can grow without new labels.
SIGNAL_KINDS = ("news", "blog", "event")

# Query keys that carry no identity — only click/campaign tracking. Stripped when
# canonicalising so the same story shared with different tracking tags dedupes to
# one Signal. `utm_*` is matched by prefix; the rest are exact (case-insensitive).
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "msclkid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "igshid",
    }
)


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in _TRACKING_PARAMS


def canonicalise_url(url: str) -> str:
    """Reduce a URL to a stable identity so the same story dedupes to one Signal.

    Pure function. Rules (see acceptance for #33):
      - force the scheme to ``https`` (and default a scheme-less URL to https);
      - lowercase the host (hostnames are case-insensitive; the path is left as-is
        because paths can be case-sensitive);
      - drop tracking query params (``utm_*``, ``fbclid``, ``gclid``, …) while
        keeping the rest in their original order;
      - drop the fragment;
      - strip a trailing slash from the path (so ``/x`` and ``/x/`` are one URL).

    Empty/whitespace input returns ``""``.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlparse(raw)
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query = urlencode(kept)
    return urlunparse(("https", netloc, path, parts.params, query, ""))


class SignalRecord(BaseModel):
    """A single news/blog/event item about one or more companies.

    Content is kept deliberately thin — ``title`` + ``summary`` only, never full
    article text (retention story #37 caps graph growth). ``url`` is stored in its
    canonical form (see :func:`canonicalise_url`); ``canonical_url`` is what the
    write path keys on.
    """

    url: str
    title: str
    published_at: str | None = None  # raw string as found; parsed to a date on write
    kind: str = "news"  # ∈ SIGNAL_KINDS
    summary: str | None = None
    source: str | None = None  # provenance: URL of the Source that yielded this

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in SIGNAL_KINDS:
            raise ValueError(f"kind must be one of {SIGNAL_KINDS}, got {v!r}")
        return v

    def canonical_url(self) -> str:
        return canonicalise_url(self.url)

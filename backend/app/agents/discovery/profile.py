"""Cohort profile builder (issue #75).

Given a seed company and its in-graph similar cohort, derive a search *profile*:
the group's shared kind / country / topics, a set of lowercase match *terms* (used
later to explain WHY a web candidate fits), and an LLM one-line summary of "what
kind of company this group is". The summary is grounded in the cohort's own stored
`about` text — it is cited to the cohort, never invented — and only ever phrases
search queries; it never steers a write.

`derive_profile_facts` is pure (rows in, facts out) so it tests without a DB or a
model. The model call is isolated behind `summarise_cohort` so callers can mock it;
`build_profile` reads the graph and stitches the two together.
"""

from collections import Counter
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from app.config import settings
from app.genai_retry import generate_with_retry
from app.graph import queries

# Human-readable category words per ecosystem kind, used to phrase queries
# ("<category> companies in <country>"). Falls back to a neutral word.
_KIND_CATEGORY = {
    "service_provider": "consultancy",
    "isv": "software",
    "cloud_provider": "cloud",
}

_MAX_TOPICS = 4


def kind_category(kind: str | None) -> str:
    """A readable category word for an ecosystem kind (for query phrasing)."""
    return _KIND_CATEGORY.get(kind or "", "company")


@dataclass
class CohortProfile:
    """The search template derived from a seed + its similar cohort."""

    seed: str
    kind: str | None
    country: str | None
    topics: list[str]
    cohort: list[str]  # the cohort company names — the citation basis for the summary
    terms: list[str] = field(default_factory=list)  # lowercase match terms for the "why"
    summary: str = ""  # LLM one-liner, grounded in the cohort's abouts

    def to_dict(self) -> dict:
        """A JSON-safe view for the job payload / review UI."""
        return {
            "seed": self.seed,
            "kind": self.kind,
            "country": self.country,
            "topics": self.topics,
            "cohort": self.cohort,
            "terms": self.terms,
            "summary": self.summary,
        }


def _dominant(values: list[str | None]) -> str | None:
    """The most common non-empty value, ties broken alphabetically (deterministic)."""
    counts = Counter(v for v in values if v)
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def derive_profile_facts(
    seed_row: dict, cohort_rows: list[dict], *, max_topics: int = _MAX_TOPICS
) -> CohortProfile:
    """Aggregate the seed + cohort into profile facts (pure — no DB, no model).

    Each row is a dict with `name`, `kind`, `hqCountry`, and `topics` (a list).
    kind/country are the group's dominant non-empty value; topics are the most
    frequent across the whole group, capped. The match `terms` combine the topics,
    the kind's category word, and the country — the vocabulary a web result must
    echo to earn a "why".
    """
    group = [seed_row, *cohort_rows]
    kind = _dominant([r.get("kind") for r in group])
    country = _dominant([r.get("hqCountry") for r in group])

    topic_counts: Counter[str] = Counter()
    for r in group:
        for t in r.get("topics") or []:
            if t:
                topic_counts[t] += 1
    topics = [t for t, _ in sorted(topic_counts.items(), key=lambda kv: (-kv[1], kv[0]))][
        :max_topics
    ]

    # Lowercase, de-duplicated match terms. Topics carry the most signal; the kind
    # category word and country round it out.
    seen: set[str] = set()
    terms: list[str] = []
    for candidate in [*topics, kind_category(kind), country or ""]:
        key = (candidate or "").strip().lower()
        if key and key != "company" and key not in seen:
            seen.add(key)
            terms.append(key)

    return CohortProfile(
        seed=seed_row.get("name", ""),
        kind=kind,
        country=country,
        topics=topics,
        cohort=[r.get("name", "") for r in cohort_rows if r.get("name")],
        terms=terms,
    )


async def summarise_cohort(seed_name: str, rows: list[dict]) -> str:
    """One grounded sentence describing what kind of company this cohort is.

    Built from the cohort's OWN stored `about` text (our graph data, not crawled
    input) so the summary is cited to the cohort. Returns "" if the model gives
    nothing back — the caller degrades to the structured facts. Charges the active
    per-run budget + shared rate limiter via `generate_with_retry`.
    """
    lines = []
    for r in rows:
        about = (r.get("about") or "").strip()
        lines.append(f"- {r.get('name', '')}: {about}" if about else f"- {r.get('name', '')}")
    prompt = (
        "The companies below are a cohort found to be similar to each other in a "
        "research graph. In ONE sentence, describe what kind of company this group "
        "is — their shared domain, services, and market — so a colleague could go "
        "find MORE companies like them. Do not name the companies; describe the "
        "type.\n\n" + "\n".join(lines)
    )
    resp = await generate_with_retry(
        genai.Client(),
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0),
    )
    return (resp.text or "").strip()


async def build_profile(driver, seed_name: str, cohort_names: list[str]) -> CohortProfile:
    """Read the seed + cohort from the graph and build the full profile (facts +
    grounded summary). The summary failing is not fatal — the structured facts
    alone still drive query generation."""
    rows = await queries.cohort_profile_rows(driver, [seed_name, *cohort_names])
    by_name = {r["name"]: r for r in rows}
    seed_row = by_name.get(seed_name, {"name": seed_name, "topics": []})
    cohort_rows = [by_name[n] for n in cohort_names if n in by_name]

    profile = derive_profile_facts(seed_row, cohort_rows)
    profile.summary = await summarise_cohort(seed_name, [seed_row, *cohort_rows])
    return profile

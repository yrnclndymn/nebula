"""Targeted query generation from a cohort profile (issue #75).

Pure: profile in, a handful of search strings out. The queries triangulate the
cohort from three angles — the category+geography ("<category> companies in
<country>"), the topics the group shares, and the seed itself ("<seed>
competitors", "companies like <seed> and <similar-1>") — so the outward search
finds more of the same kind rather than the seed alone. Bounded to a small handful
so a run stays within its search budget.
"""

from app.agents.discovery.profile import CohortProfile, kind_category

_MAX_QUERIES = 5


def build_queries(profile: CohortProfile) -> list[str]:
    """A small, deduplicated list of web-search queries derived from `profile`."""
    category = kind_category(profile.kind)
    country = profile.country
    queries: list[str] = []

    # 1. Category + geography — the broad "more of this kind, here" sweep.
    if country:
        queries.append(f"{category} companies in {country}")

    # 2. Topic-led — the group's shared subject matter, narrowed by category/geo.
    for topic in profile.topics[:2]:
        parts = [topic, f"{category} companies"]
        if country:
            parts.append(country)
        queries.append(" ".join(parts))

    # 3. Seed-relative — competitors and peers by name.
    if profile.seed:
        queries.append(f"{profile.seed} competitors")
        peer = profile.cohort[0] if profile.cohort else ""
        queries.append(
            f"companies like {profile.seed} and {peer}"
            if peer
            else f"companies like {profile.seed}"
        )

    # De-dupe (case-insensitively) while preserving order, then cap.
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        q = q.strip()
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:_MAX_QUERIES]

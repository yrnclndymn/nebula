"""Pure assembly of a committable :class:`PersonRecord` from raw research.

This is the provenance gate for the People-Intelligence write path. The research
step (a Gemini call over untrusted crawled/searched evidence) is asked to cite
every fact, but a claimed citation is not trusted: :func:`build_person_record`
keeps a fact ONLY when a citation names it *and* that citation's source is a real
``http(s)`` URL. Everything unsupported is dropped. So "no fact saved without a
citation" holds by construction — the same discipline the company CITES path uses
— and the surviving ``citations`` list is exactly the provenance that will be
written, nothing more.

Pure (no DB, no network, no model): easy to reason about and test-first.
"""

from app.agents.people.models import PersonCitation, PersonRecord, PersonResearch, PriorRole
from app.graph.person_identity import canonical_linkedin


def valid_source(url: str | None) -> bool:
    """A citation/link URL we will render and store: a real ``http(s)`` URL only.

    Guards the review surface (sources/talks become clickable links) against a
    hostile ``javascript:``/``data:`` scheme sneaking through the untrusted model
    output — the same guard the discovery candidate extractor applies.
    """
    return bool(url) and url.strip().lower().startswith(("http://", "https://"))


def _cited(citations: list[PersonCitation], field: str) -> list[PersonCitation]:
    """Citations that name ``field`` and carry a valid source URL."""
    return [c for c in citations if c.field == field and valid_source(c.source)]


def _dedup_citations(citations: list[PersonCitation]) -> list[PersonCitation]:
    """De-duplicate by (field, source), preserving first-seen order."""
    out: list[PersonCitation] = []
    seen: set[tuple[str, str]] = set()
    for c in citations:
        key = (c.field, c.source)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def build_person_record(research: PersonResearch, company: str) -> PersonRecord:
    """Derive the committable, provenance-filtered record from raw research.

    ``company`` is the scoping company (locates the :Person node on commit). Each
    scalar fact survives only if backed by a citation for that field; ``linkedin``
    is additionally canonicalised (and dropped if it isn't a personal profile);
    ``personal_site`` and ``talks`` must themselves be valid URLs; a prior role
    survives only with a named company and a valid source URL. The returned
    ``citations`` are exactly those that justified a surviving fact.
    """
    used: list[PersonCitation] = []

    def keep_scalar(field: str, value: str | None) -> str | None:
        if not value or not str(value).strip():
            return None
        matches = _cited(research.citations, field)
        if not matches:
            return None
        used.extend(matches)
        return value

    title = keep_scalar("title", research.current_title)
    bio = keep_scalar("bio", research.bio)

    # LinkedIn: canonicalise to the identity key, and require a citation for it.
    linkedin = None
    canon = canonical_linkedin(research.linkedin)
    if canon:
        matches = _cited(research.citations, "linkedin")
        if matches:
            linkedin = canon
            used.extend(matches)

    # Personal site: must be a valid URL in its own right AND be cited.
    personal_site = None
    if valid_source(research.personal_site):
        matches = _cited(research.citations, "personal_site")
        if matches:
            personal_site = research.personal_site
            used.extend(matches)

    # Talks: keep only valid-URL talks, and only if the field is cited at all.
    talks: list[str] = []
    talk_cites = _cited(research.citations, "talks")
    if talk_cites:
        talks = [t for t in research.talks if valid_source(t)]
        if talks:
            used.extend(talk_cites)

    # Prior roles carry their own per-role provenance: drop any without a named
    # company or a valid source URL. The company MERGEs as a stub on commit.
    prior_roles: list[PriorRole] = [
        r
        for r in research.prior_roles
        if r.company and r.company.strip() and valid_source(r.source)
    ]

    return PersonRecord(
        name=research.name,
        company=company,
        title=title,
        bio=bio,
        linkedin=linkedin,
        personal_site=personal_site,
        talks=talks,
        prior_roles=prior_roles,
        citations=_dedup_citations(used),
    )


def diff_person(existing: dict | None, record: PersonRecord) -> list[dict]:
    """A compact field-by-field diff for the review surface (pure).

    ``existing`` is the current :Person snapshot (see
    :func:`app.graph.person_enrichment.get_person_scoped`) or ``None`` when the
    person carries no enrichment yet. Returns one ``{field, old, new}`` entry per
    changed scalar fact, plus a ``prior_roles`` entry with the count of newly
    proposed roles. Only surfaces facts the record actually carries (i.e. cited).
    """
    existing = existing or {}
    changes: list[dict] = []

    def scalar(field: str, new, old_key: str) -> None:
        old = existing.get(old_key)
        if new and new != old:
            changes.append({"field": field, "old": old, "new": new})

    scalar("title", record.title, "title")
    scalar("bio", record.bio, "bio")
    scalar("linkedin", record.linkedin, "linkedin")
    scalar("personal_site", record.personal_site, "personalSite")

    if record.talks:
        old_talks = existing.get("talks") or []
        added = [t for t in record.talks if t not in old_talks]
        if added:
            changes.append({"field": "talks", "old": old_talks, "new": record.talks})

    if record.prior_roles:
        existing_roles = existing.get("prior_roles") or []
        existing_keys = {(r.get("company"), r.get("title")) for r in existing_roles}
        added = [r for r in record.prior_roles if (r.company, r.title) not in existing_keys]
        if added:
            changes.append(
                {
                    "field": "prior_roles",
                    "old": len(existing_roles),
                    "new": [r.model_dump() for r in record.prior_roles],
                }
            )

    return changes

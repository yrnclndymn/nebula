"""Diff a proposed enrichment against what the graph already holds.

A re-research produces a full `CompanyRecord`, but the user usually asked about one
thing ("update the headcount") or wants to see *what changed* — not the whole card
again. This module turns (existing record, proposed record) into a structured diff
the chat UI renders: per scalar field whether the value is new / changed / same, and
for the list relationships (clients, partners) just the additions. Leadership is
handled separately by `reconcile.py` and passed through here.
"""

from app.agents.assistant.reconcile import _norm

# (record key [snake], graph key [camel], display label, focus aliases).
# The record key is also the CompanyRecord field, so a focused commit can write it
# directly. Aliases let us resolve a free-text focus ("employees" → headcount).
FIELDS: list[tuple[str, str, str, set[str]]] = [
    (
        "headcount",
        "headcount",
        "Headcount",
        {"headcount", "employees", "employee", "staff", "size", "team size", "people"},
    ),
    (
        "hq_location",
        "hqLocation",
        "HQ",
        {"hq", "headquarters", "location", "office", "address", "based"},
    ),
    (
        "year_founded",
        "yearFounded",
        "Founded",
        {"founded", "year founded", "founding", "inception", "established"},
    ),
    (
        "funding",
        "funding",
        "Funding",
        {"funding", "investment", "raised", "capital", "funding round", "investors"},
    ),
    (
        "estimated_revenue",
        "estimatedRevenue",
        "Revenue",
        {"revenue", "estimated revenue", "turnover", "sales"},
    ),
    ("about", "about", "About", {"about", "description", "summary", "overview"}),
    ("website", "website", "Website", {"website", "url", "site", "homepage"}),
    ("linkedin", "linkedin", "LinkedIn", {"linkedin", "linkedin url", "linkedin profile"}),
]


def _slug(text: str | None) -> str:
    """Normalize a focus/citation label for matching: casefold, spaces for _/-."""
    if not text:
        return ""
    return _norm(text.replace("_", " ").replace("-", " "))


def resolve_focus(focus: str | None) -> str | None:
    """Map a free-text focus ("headcount", "hq", "employees") to a record key, or
    None if it doesn't name a single scalar field we can scope a commit to."""
    slug = _slug(focus)
    if not slug:
        return None
    for record_key, _graph_key, _label, aliases in FIELDS:
        if slug == _slug(record_key) or slug in {_slug(a) for a in aliases}:
            return record_key
    return None


def field_label(record_key: str) -> str:
    for key, _graph_key, label, _aliases in FIELDS:
        if key == record_key:
            return label
    return record_key


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == 0


def _same_value(old: object, new: object) -> bool:
    if isinstance(old, (int, float)) or isinstance(new, (int, float)):
        try:
            return int(old) == int(new)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    return _norm(str(old)) == _norm(str(new))


def _list_diff(existing_items: list, proposed_items: list) -> dict:
    have = {_norm(str(x)) for x in existing_items}
    added = [x for x in proposed_items if _norm(str(x)) not in have]
    return {"added": added, "existing_count": len(existing_items)}


def compute_diff(existing: dict | None, record: dict, leadership: dict) -> dict:
    """Build the structured diff. `record` is a CompanyRecord.model_dump();
    `leadership` is the reconcile_people() result (added/merged/variants)."""
    scalars = []
    for record_key, graph_key, label, _aliases in FIELDS:
        new = record.get(record_key)
        if _is_empty(new):
            continue  # research turned up nothing for this field — omit it
        old = existing.get(graph_key) if existing else None
        if existing is None or _is_empty(old):
            status = "new"
        elif _same_value(old, new):
            status = "same"
        else:
            status = "changed"
        scalars.append(
            {"key": record_key, "label": label, "old": old, "new": new, "status": status}
        )

    return {
        "scalars": scalars,
        "clients": _list_diff(
            existing.get("clients", []) if existing else [], record.get("clients", [])
        ),
        "partners": _list_diff(
            existing.get("partners", []) if existing else [], record.get("partnerships", [])
        ),
        "leadership": {
            "added": leadership.get("added", []),
            "merged": leadership.get("merged", []),
            "variants": leadership.get("variants", []),
        },
    }


def citation_matches_focus(citation_field: str | None, focus_key: str) -> bool:
    """Does a citation's `field` justify the focused field? Matches on the key or
    any of its aliases (the model doesn't always name the field exactly)."""
    return _slug(citation_field) == _slug(focus_key) or resolve_focus(citation_field) == focus_key

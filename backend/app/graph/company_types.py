"""Controlled vocabulary for company ownership/structure types (story #103).

Lives in the graph layer because it governs what labels the graph ever stores — a
company-domain concern shared by BOTH write paths (the CSV importer's LLM extractor
and the enrichment agent's ``save_company`` tool). Keeping it here lets ``tools`` and
``importer`` depend on it downward, instead of ``tools`` reaching up into
``importer`` (the layering violation this move fixes).

Anything not mapping here is dropped, so the graph only ever sees these canonical
labels (no casing dupes, no generic legal forms).
"""

_COMPANY_TYPE_CANON = {
    "b-corp": "B-Corp",
    "bcorp": "B-Corp",
    "b corp": "B-Corp",
    "certified b-corp": "B-Corp",
    "esop": "ESOP",
    "employee-owned": "employee-owned",
    "employee owned": "employee-owned",
    "co-op": "co-operative",
    "coop": "co-operative",
    "cooperative": "co-operative",
    "co-operative": "co-operative",
    "non-profit": "non-profit",
    "nonprofit": "non-profit",
    "not-for-profit": "non-profit",
    "pbc": "PBC",
    "public benefit corporation": "PBC",
    "public-benefit corporation": "PBC",
    "public benefit corp": "PBC",
    "benefit corporation": "PBC",
    "foundation-owned": "foundation-owned",
    "foundation owned": "foundation-owned",
}


def canonical_company_types(raw: list[str]) -> list[str]:
    """Map extracted types to the controlled vocabulary; drop everything else."""
    out: list[str] = []
    for value in raw:
        canon = _COMPANY_TYPE_CANON.get(value.strip().lower())
        if canon and canon not in out:
            out.append(canon)
    return out

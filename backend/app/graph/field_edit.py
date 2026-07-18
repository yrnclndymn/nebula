"""User-initiated inline field edits: pure validation + provenance write (#149).

The companies table lets the user set three scalar fields by hand. The repo's
provenance rule holds even for a human edit — headcount and funding (a headcount
or financial figure) require a source URL; yearFounded may be set without one.
Each saved value tags the CITES edge `origin='user'` and dedupe-appends the field
to the Company's `userEdited` list, so later enrichment can tell a hand-set value
from an agent-found one (and eventually respect it).

The human is the reviewer here (the direct-write precedent is `set_company_kind`),
so there is no propose→review→commit ceremony — the agent-only guardrail that
keeps *agents* off a direct-write path is untouched.
"""

from dataclasses import dataclass
from urllib.parse import urlparse

from neo4j import AsyncDriver

# field -> (value kind, source-URL required). The provenance rule (no headcount
# or financial figure without a citation) is what forces source on two of three.
EDITABLE_FIELDS: dict[str, tuple[str, bool]] = {
    "headcount": ("int", True),
    "yearFounded": ("int", False),
    "funding": ("text", True),
}

# Plausible founding-year window — guards against typos / bad coercion.
YEAR_MIN = 1600
YEAR_MAX = 2100


class FieldEditError(ValueError):
    """A validation failure the route maps to HTTP 422."""


@dataclass(frozen=True)
class ValidatedEdit:
    """A field edit that passed validation — safe to write."""

    field: str
    value: int | str
    source_url: str | None


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _coerce_int(value: object, field: str) -> int:
    # bool is an int subclass — reject it explicitly so True/False can't sneak in.
    if isinstance(value, bool):
        raise FieldEditError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise FieldEditError(f"{field} must be an integer")
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        try:
            return int(cleaned)
        except ValueError:
            raise FieldEditError(f"{field} must be an integer") from None
    raise FieldEditError(f"{field} must be an integer")


def validate_field_edit(field: str, value: object, source_url: object) -> ValidatedEdit:
    """Validate + coerce a `{field, value, source_url}` edit.

    Raises FieldEditError on: an off-allowlist field, a non-coercible/implausible
    value, a non-http(s) source URL, or a missing source URL where the field
    requires one. Returns a ValidatedEdit ready for `apply_field_edit`.
    """
    if field not in EDITABLE_FIELDS:
        raise FieldEditError(f"field must be one of {sorted(EDITABLE_FIELDS)}")
    kind, requires_source = EDITABLE_FIELDS[field]

    source = source_url.strip() if isinstance(source_url, str) else source_url
    if source == "":
        source = None
    if source is not None and not _is_http_url(source):
        raise FieldEditError("source_url must be an http(s) URL")
    if requires_source and source is None:
        raise FieldEditError(f"{field} requires a source URL")

    if kind == "int":
        coerced = _coerce_int(value, field)
        if field == "yearFounded" and not (YEAR_MIN <= coerced <= YEAR_MAX):
            raise FieldEditError(f"yearFounded must be between {YEAR_MIN} and {YEAR_MAX}")
        if field == "headcount" and coerced < 0:
            raise FieldEditError("headcount must be a non-negative integer")
        out: int | str = coerced
    else:  # text
        if not isinstance(value, str) or not value.strip():
            raise FieldEditError(f"{field} must be non-empty text")
        out = value.strip()

    return ValidatedEdit(field=field, value=out, source_url=source)


async def apply_field_edit(driver: AsyncDriver, name: str, edit: ValidatedEdit) -> bool:
    """Write a validated user edit.

    Sets the scalar property, dedupe-appends the field to `c.userEdited`, and —
    when a source URL is present — MERGEs the `(c)-[:CITES {field}]->(:Source)`
    edge with `origin='user'` (same edge shape as the agent citation write in
    `repository.py`). The field's PRIOR citation edges are deleted first: a user
    edit supersedes whatever previously justified the value, and without the
    delete each re-edit with a new source would stack another CITES edge that
    the company-detail read then shows alongside the current one (PR #160
    review). Returns False when no such company exists (→ 404).
    """

    async def _tx(tx) -> bool:
        # One transaction for both statements (the upsert_company precedent): a
        # crash between "delete old citation + set value" and "write new
        # citation" would otherwise commit a headcount/funding figure with NO
        # citation — the exact state the provenance guardrail forbids
        # (PR #160 review).
        result = await tx.run(
            "MATCH (c:Company {name: $name}) "
            "SET c += $props, c.updatedAt = datetime(), "
            "    c.userEdited = [f IN coalesce(c.userEdited, []) WHERE f <> $field] + $field "
            "WITH c "
            "OPTIONAL MATCH (c)-[old:CITES {field: $field}]->() "
            "DELETE old "
            "RETURN DISTINCT c.name AS name",
            name=name,
            props={edit.field: edit.value},
            field=edit.field,
        )
        if await result.single() is None:
            return False
        if edit.source_url is not None:
            await tx.run(
                "MATCH (c:Company {name: $name}) "
                "MERGE (s:Source {url: $source}) "
                "MERGE (c)-[r:CITES {field: $field}]->(s) "
                "SET r.value = $value, r.capturedAt = datetime(), r.origin = 'user'",
                name=name,
                source=edit.source_url,
                field=edit.field,
                value=str(edit.value),
            )
        return True

    async with driver.session() as session:
        return await session.execute_write(_tx)

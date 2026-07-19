"""Person identity keyed on a canonical LinkedIn URL (story #39).

Person nodes were historically deduped by name only. That is brittle: name
variants ("Andy"/"Andrew") split one person across two nodes, while genuine
namesakes collapse two people into one. Where a person's LinkedIn profile is
known it is a far stronger identity, so a Person is re-keyed on its *canonical*
LinkedIn URL and the name becomes display-only.

This module holds the name-matching evidence + the merge migration. The identity
key itself and its graph mutation moved DOWN to the graph layer (#183) and are
re-exported here so this module's public surface is unchanged:

- :func:`canonical_linkedin` (now ``app.graph.linkedin``) reduces any LinkedIn
  *personal-profile* URL to one stable key — scheme, www/country/mobile subdomain,
  trailing slash, query, fragment and slug case are all normalised away — so
  trailing-slash / case / ``m.linkedin`` variants can never mint two identities for
  one person. It moved below the domain so the committable record models can
  canonicalise their ``linkedin`` field in a validator (the single choke point)
  without importing UP into ``people``.
- :func:`attach_linkedin` (now ``app.graph.person_enrichment``) is the reviewable
  commit that attaches a discovered URL to an existing name-only person; pure
  Cypher, so it belongs in the graph layer next to the write path that calls it.
- :func:`linkedin_slug_matches_name` / :func:`extract_person_linkedins` are the
  deterministic evidence used by the discovery step (``scripts/``) so a crawled
  or searched URL is only ever attached to a person on a strong, checkable match
  — never on page proximity alone (crawled content is untrusted).
- :func:`migrate_person_identity` canonicalises LinkedIn values already stored on
  Person nodes and merges any nodes that now share a canonical URL. Idempotent,
  dry-run-able, and it reports every merge.

The unique constraint on ``Person.linkedin`` (see ``schema.py``) enforces the key
at the DB. Neo4j property-uniqueness ignores nulls, so name-only people are
unaffected. **Ordering:** run this migration *before* the constraint is applied to
already-dirty data — two nodes may store the same canonical value under different
raw spellings, which the constraint would reject at creation time; the migration
merges them first.
"""

import asyncio
import re

from neo4j import AsyncDriver

# `canonical_linkedin` (pure key) and `attach_linkedin` (its graph mutation, with
# the `_merge_group_tx` helper) now live in the graph layer (#183): the record
# models canonicalise in a validator, so the key belongs below the domain, and the
# attach is pure Cypher. They are re-exported here so the discovery domain
# (`person_discovery`) and the migration CLI below keep their existing import from
# this module — and the deleted upward-import pins stay deleted.
from app.graph.linkedin import canonical_linkedin
from app.graph.person_enrichment import _merge_group_tx, attach_linkedin

__all__ = [
    "attach_linkedin",
    "canonical_linkedin",
    "extract_person_linkedins",
    "linkedin_slug_matches_name",
    "migrate_person_identity",
]

_HREF_RE = re.compile(r'href=["\']([^"\'#\s]+)["\']', re.I)
_SLUG_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _slug_of(url_or_slug: str) -> str:
    """The ``/in/`` slug from a URL, or the input itself if it is already a slug."""
    canon = canonical_linkedin(url_or_slug)
    if canon:
        return canon.rsplit("/", 1)[-1]
    return url_or_slug.strip().lower()


def _name_tokens(name: str) -> list[str]:
    return [t for t in _SLUG_TOKEN_RE.split(name.lower()) if t]


def linkedin_slug_matches_name(url_or_slug: str, name: str) -> bool:
    """Deterministic evidence that a ``/in/`` slug belongs to ``name``.

    True only when the slug contains BOTH the person's first and last name tokens
    (order-independent, case-insensitive). A trailing numeric disambiguation hash
    on the slug is ignored. Requiring both tokens keeps precision high — a
    surname-only or first-name-only overlap is never enough, so an untrusted
    crawled/searched URL is only attached on a strong match.
    """
    name_tokens = _name_tokens(name)
    if len(name_tokens) < 2:
        return False  # a single-token display name is never confident evidence
    first, last = name_tokens[0], name_tokens[-1]
    slug_tokens = {t for t in _SLUG_TOKEN_RE.split(_slug_of(url_or_slug)) if t and not t.isdigit()}
    return first in slug_tokens and last in slug_tokens


def extract_person_linkedins(html: str) -> set[str]:
    """Canonical personal-profile URLs (``/in/<slug>``) found in a page's hrefs.

    Company/school pages and non-profile links are dropped (``canonical_linkedin``
    returns ``None`` for them). Deduped by canonical form.
    """
    out: set[str] = set()
    for href in _HREF_RE.findall(html or ""):
        canon = canonical_linkedin(href)
        if canon:
            out.add(canon)
    return out


# --- Merge migration --------------------------------------------------------


async def _person_rows(driver: AsyncDriver) -> list[dict]:
    """Every Person carrying a linkedin value, with its LEADS degree for survivor
    selection. ``eid`` (elementId) is the stable handle we merge/delete on, since
    two rows can share a name."""
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person) WHERE p.linkedin IS NOT NULL
            OPTIONAL MATCH (p)-[r:LEADS]->()
            RETURN elementId(p) AS eid, p.name AS name, p.linkedin AS linkedin,
                   count(r) AS leads
            ORDER BY name
            """
        )
        return [dict(record) async for record in result]


def _plan_merges(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group Person rows by canonical LinkedIn URL and plan the writes.

    Pure so it is easy to reason about and test. Returns ``(canonicalise, merges)``:
      - canonicalise: rows whose stored ``linkedin`` differs from its canonical
        form (rewrite-in-place), for groups of exactly one node.
      - merges: ``{canonical, survivor_eid, survivor_name, absorbed:[{eid,name}]}``
        for each canonical URL shared by >1 node. The survivor is the node with
        the most LEADS edges, then a non-empty name, then a stable eid.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        canon = canonical_linkedin(row["linkedin"])
        if canon is None:
            continue  # a stored value that isn't a personal profile — leave it be
        groups.setdefault(canon, []).append(row)

    canonicalise: list[dict] = []
    merges: list[dict] = []
    for canon, members in groups.items():
        if len(members) == 1:
            only = members[0]
            if only["linkedin"] != canon:
                canonicalise.append({"eid": only["eid"], "name": only["name"], "canonical": canon})
            continue
        survivor = sorted(members, key=lambda m: (-m["leads"], m["name"] in (None, ""), m["eid"]))[
            0
        ]
        absorbed = [
            {"eid": m["eid"], "name": m["name"]} for m in members if m["eid"] != survivor["eid"]
        ]
        merges.append(
            {
                "canonical": canon,
                "survivor_eid": survivor["eid"],
                "survivor_name": survivor["name"],
                "absorbed": absorbed,
            }
        )
    return canonicalise, merges


async def migrate_person_identity(driver: AsyncDriver, *, dry_run: bool = True) -> dict:
    """Canonicalise stored Person LinkedIn URLs and merge same-URL duplicates.

    Idempotent — safe to re-run; a clean graph reports zero changes. Reports every
    merge so the operator sees exactly which nodes collapsed. With ``dry_run`` the
    plan is computed and returned but nothing is written.
    """
    rows = await _person_rows(driver)
    canonicalise, merges = _plan_merges(rows)

    if not dry_run:
        async with driver.session() as session:
            for change in canonicalise:
                await session.run(
                    "MATCH (p:Person) WHERE elementId(p) = $eid SET p.linkedin = $canonical",
                    eid=change["eid"],
                    canonical=change["canonical"],
                )
            for merge in merges:
                await session.execute_write(_merge_group_tx, merge)

    return {
        "dry_run": dry_run,
        "people_with_linkedin": len(rows),
        "canonicalised": canonicalise,
        "merges": merges,
    }


def _print_report(report: dict) -> None:
    verb = "would canonicalise" if report["dry_run"] else "canonicalised"
    for change in report["canonicalised"]:
        print(f"  {verb}: {change['name']} -> {change['canonical']}")
    verb = "would merge" if report["dry_run"] else "merged"
    for merge in report["merges"]:
        names = ", ".join(a["name"] or "(unnamed)" for a in merge["absorbed"])
        print(
            f"  {verb} into {merge['survivor_name'] or '(unnamed)'} [{merge['canonical']}]: {names}"
        )
    print(
        f"\n{len(report['canonicalised'])} URL(s) {'to canonicalise' if report['dry_run'] else 'canonicalised'}, "
        f"{len(report['merges'])} duplicate-group(s) {'to merge' if report['dry_run'] else 'merged'} "
        f"across {report['people_with_linkedin']} person node(s) with a LinkedIn URL."
    )


async def _main() -> None:
    import argparse

    from app.graph.driver import close_driver, get_driver

    parser = argparse.ArgumentParser(
        description="Re-key Person identity on canonical LinkedIn URL."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="apply the migration (default: dry-run report only)",
    )
    args = parser.parse_args()

    driver = get_driver()
    try:
        report = await migrate_person_identity(driver, dry_run=not args.commit)
        _print_report(report)
    finally:
        await close_driver()


if __name__ == "__main__":
    asyncio.run(_main())

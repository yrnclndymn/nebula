"""Person identity keyed on a canonical LinkedIn URL (story #39).

Person nodes were historically deduped by name only. That is brittle: name
variants ("Andy"/"Andrew") split one person across two nodes, while genuine
namesakes collapse two people into one. Where a person's LinkedIn profile is
known it is a far stronger identity, so a Person is re-keyed on its *canonical*
LinkedIn URL and the name becomes display-only.

This module holds the pure pieces plus the merge migration:

- :func:`canonical_linkedin` reduces any LinkedIn *personal-profile* URL to one
  stable key — scheme, www/country/mobile subdomain, trailing slash, query,
  fragment and slug case are all normalised away — so trailing-slash / case /
  ``m.linkedin`` variants can never mint two identities for one person. Company
  and school pages, and non-LinkedIn URLs, return ``None``: they are not a
  person's identity and must never be rewritten into a fake profile.
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
from urllib.parse import urlparse

from neo4j import AsyncDriver, AsyncManagedTransaction

from app.tools.social import normalize_linkedin

_HREF_RE = re.compile(r'href=["\']([^"\'#\s]+)["\']', re.I)
_SLUG_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def canonical_linkedin(url: str | None) -> str | None:
    """Reduce a LinkedIn *personal-profile* URL to its canonical identity key.

    Returns ``https://www.linkedin.com/in/<slug>`` (slug lower-cased) for a
    personal profile, or ``None`` for empty input, a company/school page, a bare
    LinkedIn host, or any non-LinkedIn URL. Pure and idempotent.
    """
    if not url or not url.strip():
        return None
    # normalize_linkedin already handles scheme, www/country/mobile subdomain,
    # query, fragment and trailing slash; it returns a non-LinkedIn URL unchanged.
    normalized = normalize_linkedin(url.strip())
    parsed = urlparse(normalized)
    if parsed.netloc.lower() != "www.linkedin.com":
        return None  # normalize left it untouched -> not a LinkedIn URL
    # Only a personal profile (/in/<slug>) identifies a person.
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "in":
        return None
    slug = parts[1].lower()  # LinkedIn slugs are case-insensitive
    return f"https://www.linkedin.com/in/{slug}" if slug else None


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


async def _merge_group_tx(tx: AsyncManagedTransaction, merge: dict) -> None:
    """Fold every absorbed node into the survivor, then set the canonical URL.

    Re-points each absorbed node's LEADS edges (carrying the title through), fills
    the survivor's name if it was empty, DETACH DELETEs the absorbed node, and only
    THEN writes the canonical linkedin onto the survivor — deleting the duplicates
    first is what keeps the write clear of the uniqueness constraint.
    """
    absorbed_eids = [a["eid"] for a in merge["absorbed"]]
    # Re-point LEADS from the absorbed nodes onto the survivor (keep any title).
    await tx.run(
        """
        MATCH (survivor:Person) WHERE elementId(survivor) = $survivor
        MATCH (dup:Person)-[r:LEADS]->(c:Company)
        WHERE elementId(dup) IN $absorbed
        MERGE (survivor)-[nr:LEADS]->(c)
        SET nr.title = coalesce(nr.title, r.title)
        DELETE r
        """,
        survivor=merge["survivor_eid"],
        absorbed=absorbed_eids,
    )
    # Fill the survivor's display name from an absorbed node if it lacks one.
    await tx.run(
        """
        MATCH (survivor:Person) WHERE elementId(survivor) = $survivor
        OPTIONAL MATCH (dup:Person)
          WHERE elementId(dup) IN $absorbed AND dup.name IS NOT NULL AND dup.name <> ''
        WITH survivor, collect(dup.name)[0] AS dupName
        SET survivor.name = coalesce(survivor.name, dupName)
        """,
        survivor=merge["survivor_eid"],
        absorbed=absorbed_eids,
    )
    # Remove the now-stripped duplicates.
    await tx.run(
        "MATCH (dup:Person) WHERE elementId(dup) IN $absorbed DETACH DELETE dup",
        absorbed=absorbed_eids,
    )
    # Finally write the canonical URL onto the sole surviving node.
    await tx.run(
        "MATCH (survivor:Person) WHERE elementId(survivor) = $survivor "
        "SET survivor.linkedin = $canonical",
        survivor=merge["survivor_eid"],
        canonical=merge["canonical"],
    )


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


async def attach_linkedin(
    driver: AsyncDriver, name: str, url: str, *, company: str, dry_run: bool = True
) -> dict:
    """Attach a discovered canonical LinkedIn URL to the name-only Person(s) called
    ``name`` who lead ``company``. The reviewable commit for enrichment-discovered
    URLs on EXISTING people (story #39) — never called silently from a write path.

    The evidence behind a discovered URL is specific to ONE company (its team page,
    its slug-gated search), so candidates are scoped to that company's leaders —
    a genuine namesake leading an unrelated company is never touched (#87 review).
    Only nodes that currently have no ``linkedin`` are considered, so a person
    already keyed on a profile is never overwritten. If a node already holds the
    canonical URL, the scoped name-only node(s) merge into it (dedup); otherwise
    the URL is set on one node and same-company name-siblings (true duplicates)
    fold into it. Returns the action taken.
    """
    canon = canonical_linkedin(url)
    if canon is None:
        return {"name": name, "action": "skipped", "reason": "not a personal-profile URL"}

    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person {name: $name})-[:LEADS]->(:Company {name: $company})
            WHERE p.linkedin IS NULL
            WITH DISTINCT p
            OPTIONAL MATCH (p)-[r:LEADS]->()
            RETURN elementId(p) AS eid, count(r) AS leads
            """,
            name=name,
            company=company,
        )
        candidates = [dict(rec) async for rec in result]
        if not candidates:
            return {
                "name": name,
                "action": "skipped",
                "reason": f"no name-only Person leading {company!r} to attach",
            }

        # An existing node may already own this canonical URL (e.g. a prior run).
        result = await session.run(
            "MATCH (p:Person {linkedin: $canon}) RETURN elementId(p) AS eid LIMIT 1", canon=canon
        )
        holder = await result.single()

        if holder is not None:
            survivor_eid, survivor_name = holder["eid"], None
            absorbed = [{"eid": c["eid"], "name": name} for c in candidates]
        else:
            keep = sorted(candidates, key=lambda c: (-c["leads"], c["eid"]))[0]
            survivor_eid, survivor_name = keep["eid"], name
            absorbed = [
                {"eid": c["eid"], "name": name} for c in candidates if c["eid"] != keep["eid"]
            ]

        action = "merged" if (holder is not None or absorbed) else "set"
        if not dry_run:
            merge = {
                "canonical": canon,
                "survivor_eid": survivor_eid,
                "survivor_name": survivor_name,
                "absorbed": absorbed,
            }
            await session.execute_write(_merge_group_tx, merge)

    return {"name": name, "canonical": canon, "action": action, "dry_run": dry_run}


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
    parser.add_argument("--dry-run", action="store_true", help="show the plan without writing")
    args = parser.parse_args()

    driver = get_driver()
    try:
        report = await migrate_person_identity(driver, dry_run=args.dry_run)
        _print_report(report)
    finally:
        await close_driver()


if __name__ == "__main__":
    asyncio.run(_main())

"""Entity resolution over company stubs: dedup, alias, junk-flag.

Enrichment pulls in partner/client organisations as bare `:Company` stubs
(MERGE-by-name, no topic, no website). Over time the same organisation shows up
under several spellings ("Acme", "Acme Inc", "Acme, LLC") and some non-company
noise sneaks in from logo/text extraction. Researching those duplicates wastes
money, so this module lets a human:

  - **merge** variant stubs into a canonical node (relationships re-pointed,
    properties unioned, provenance kept, variant names recorded as aliases);
  - **alias** extra spellings onto a canonical without a node to delete
    (API-only for now — the review modal emits merge/junk; the endpoint is
    ready if a UI wants it);
  - **junk-flag** a node so it drops out of the company/backlog lists.

Nothing here writes on its own. Detection (`detect_variant_clusters`) only
*proposes* clusters; the mutating ops (`merge_companies`, `add_aliases`,
`flag_junk`) run behind the propose→review→commit job in
`app.agents.assistant.resolution`, so a person always disposes. Merges are
irreversible graph surgery — they must only happen on an explicit user commit.

The heuristics are deliberately pure (str in, clusters out) so they test without
a database; the graph ops skip in CI when Neo4j is absent, like the rest of the
graph layer.
"""

import re

from neo4j import AsyncDriver, AsyncManagedTransaction

# Legal-form / corporate-structure tokens that don't distinguish one org from
# another ("Acme Ltd" and "Acme Inc" are the same Acme). Stripped during
# normalisation so those spellings collapse to a shared key. Kept conservative:
# only forms that are genuinely noise, not descriptive words like "bank"/"labs".
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "ltd",
        "limited",
        "plc",
        "co",
        "corp",
        "corporation",
        "company",
        "gmbh",
        "ag",
        "kg",
        "sarl",
        "sa",
        "sas",
        "srl",
        "spa",
        "bv",
        "nv",
        "oy",
        "ab",
        "as",
        "pty",
        "pte",
        "kk",
    }
)

# Common joiners dropped so "Acme & Sons" ~ "Acme and Sons".
_STOPWORDS = frozenset({"the", "and"})

# Generic tokens that many *unrelated* organisations share — geographic/directional
# descriptors and common sector words. A lone such token is not enough to anchor a
# containment merge: on its own, "Central" would chain a health body, a logistics
# firm and a food company into one cluster (issue #67 — an observed prod false
# positive). A distinctive token ("Globex", "Initech") still anchors a merge; a
# generic one needs a second shared token to corroborate. Curated (not a full
# dictionary) — kept to tokens that recur across genuinely different orgs.
_GENERIC_TOKENS = frozenset(
    {
        # Direction / geography
        "north",
        "south",
        "east",
        "west",
        "northern",
        "southern",
        "eastern",
        "western",
        "central",
        "national",
        "international",
        "global",
        "united",
        "american",
        "european",
        "british",
        "pacific",
        "atlantic",
        "metro",
        "metropolitan",
        "city",
        "greater",
        "royal",
        "new",
        # Sector / descriptor
        "health",
        "healthcare",
        "care",
        "medical",
        "food",
        "foods",
        "group",
        "holding",
        "holdings",
        "partners",
        "associates",
        "services",
        "service",
        "solutions",
        "systems",
        "technologies",
        "technology",
        "tech",
        "digital",
        "data",
        "media",
        "capital",
        "ventures",
        "financial",
        "finance",
        "bank",
        "insurance",
        "energy",
        "power",
        "retail",
        "consulting",
        "logistics",
        "industries",
        "industrial",
        "enterprises",
        "enterprise",
        "trust",
        "council",
        "authority",
        "board",
        "association",
        "foundation",
        "institute",
        "network",
        "labs",
        "studio",
        "studios",
        "works",
    }
)

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalized_tokens(name: str) -> list[str]:
    """Lowercase, de-punctuate, drop legal-form suffixes and joiner stopwords.

    "Acme, LLC" -> ["acme"]; "The Globex Company" -> ["globex"];
    "Acme & Sons GmbH" -> ["acme", "sons"].
    """
    lowered = _PUNCT.sub(" ", name.lower())
    tokens = [t for t in lowered.split() if t and t not in _STOPWORDS]
    stripped = [t for t in tokens if t not in _LEGAL_SUFFIXES]
    # If a name is *only* legal/stop tokens (e.g. "The Company"), keep the raw
    # tokens so it still has a key instead of collapsing to empty.
    return stripped or tokens


def normalize_name(name: str) -> str:
    """A canonical comparison key: normalised tokens joined by a single space."""
    return " ".join(normalized_tokens(name))


# Junk heuristics: things that are almost certainly not a company name, pulled in
# from logo/alt-text extraction. Conservative — these only *suggest* a junk flag
# for human review, never auto-apply.
_JUNK_PHRASES = re.compile(
    r"\b(read more|learn more|click here|view all|see more|our clients|"
    r"case study|case studies|privacy policy|cookie|terms of service|"
    r"all rights reserved|copyright|home page|contact us|sign up|log ?in)\b"
)


def looks_like_junk(name: str) -> bool:
    """Heuristic: does this stub name look like extraction noise, not an org?

    Flags UI/boilerplate phrases, empty/degenerate names, and pure numbers.
    Deliberately narrow to avoid false positives on real short brand names.
    """
    stripped = name.strip()
    if not stripped:
        return True
    if _JUNK_PHRASES.search(stripped.lower()):
        return True
    key = normalize_name(stripped)
    if not key:  # nothing left after normalisation -> not a real name
        return True
    return key.isdigit()  # a bare number is not an organisation


def _is_distinctive(token: str) -> bool:
    """A single token strong enough to anchor a containment merge on its own:
    at least 4 chars and not a generic geographic/sector word."""
    return len(token) >= 4 and token not in _GENERIC_TOKENS


def _containment_ok(shared: frozenset[str]) -> bool:
    """Is a containment (subset) match strong enough to propose a merge?

    Requires MORE than a single shared *generic* token (issue #67): either
      - >= 2 shared tokens (an exact multi-word prefix — strong corroboration), or
      - exactly one shared token that is distinctive (long and non-generic).
    A lone generic token ("Central", "Northern", "Health") never chains otherwise
    distinct organisations together.
    """
    if len(shared) >= 2:
        return True
    if len(shared) == 1:
        return _is_distinctive(next(iter(shared)))
    return False


def _pick_canonical(names: list[str]) -> str:
    """Choose the survivor for a cluster: the most descriptive spelling.

    Prefer the most normalised tokens (most specific), then the longest raw
    string, then alphabetical — a stable, explainable default the reviewer can
    override before committing.
    """
    return sorted(names, key=lambda n: (-len(normalized_tokens(n)), -len(n), n))[0]


def detect_variant_clusters(names: list[str]) -> list[dict]:  # noqa: C901 — union-find over pairwise variant edges is one cohesive connected-components algorithm; splitting the edge test from the merge/label pass would scatter its logic and invite subtle bugs
    """Group names that are plausibly the same organisation, for human review.

    Two signals, both conservative:
      - **normalized** equality: identical after suffix/punct stripping
        ("Acme Inc" == "Acme, LLC");
      - **containment**: one name's token set is a strict subset of another's
        AND that shared subset is strong enough to merge on (`_containment_ok`):
        >= 2 shared tokens, or a single *distinctive* (long, non-generic) token
        ("Acme" within "Acme Digital"). A lone generic token ("Central") never
        chains unrelated orgs.

    Clusters are connected components over those edges. Returns one dict per
    cluster of >= 2 names: {canonical, members, reason}. `reason` is "normalized"
    if every edge is exact, else "containment" (flagging the looser match so the
    reviewer looks harder). Nothing is merged here — the caller reviews first.
    """
    # De-dupe while preserving first-seen order for stable output.
    unique: list[str] = []
    seen: set[str] = set()
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)

    keys = {n: normalize_name(n) for n in unique}
    token_sets = {n: frozenset(normalized_tokens(n)) for n in unique}

    # Union-find over names.
    parent = {n: n for n in unique}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    # Track whether any edge in a component came from the looser containment rule.
    containment_edge: set[str] = set()

    for i, a in enumerate(unique):
        for b in unique[i + 1 :]:
            ka, kb = keys[a], keys[b]
            if not ka or not kb:
                continue
            if ka == kb:
                union(a, b)
                continue
            ta, tb = token_sets[a], token_sets[b]
            if ta < tb or tb < ta:
                # The strict subset IS the set of shared tokens. A shared set of a
                # single generic token ("Central") is too weak to merge on — it
                # would chain unrelated orgs — so gate on `_containment_ok`.
                shared = ta if ta < tb else tb
                if _containment_ok(shared):
                    union(a, b)
                    containment_edge.add(a)
                    containment_edge.add(b)

    groups: dict[str, list[str]] = {}
    for n in unique:
        groups.setdefault(find(n), []).append(n)

    clusters: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        loose = any(m in containment_edge for m in members)
        canonical = _pick_canonical(members)
        clusters.append(
            {
                "canonical": canonical,
                "members": sorted(members),
                "reason": "containment" if loose else "normalized",
            }
        )
    # Show tighter (normalized) clusters first, then by size.
    clusters.sort(key=lambda c: (c["reason"] != "normalized", -len(c["members"]), c["canonical"]))
    return clusters


async def list_stub_companies(driver: AsyncDriver) -> list[dict]:
    """Stub companies: no research topic, no website, not already junk-flagged.

    These are the merge/alias/junk candidates. Edge counts give the reviewer a
    sense of how connected each stub is (a well-connected node is a better merge
    target than a bare one).
    """
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Company)
            WHERE NOT (c)-[:TAGGED_AS]->(:Topic)
              AND c.website IS NULL
              AND NOT coalesce(c.junk, false)
            OPTIONAL MATCH (c)-[r]-()
            RETURN c.name AS name, coalesce(c.aliases, []) AS aliases, count(r) AS edges
            ORDER BY name
            """
        )
        return [dict(record) async for record in result]


async def list_client_stub_candidates(driver: AsyncDriver) -> list[dict]:
    """Stubs whose ONLY graph signal is being someone's client → propose kind='client'.

    An end-customer organisation (a bank/retailer/public body dragged in via
    HAS_CLIENT) typically enters the graph as a bare stub that is *only* ever the
    object of a HAS_CLIENT edge: nobody's partner, no clients of its own, no
    leadership, no topic tag, no company-type tag, no website. That exact shape is
    what this heuristic isolates.

    The query is deliberately strict — it proposes only, and only for stubs with
    no other signal:
      - `c.kind IS NULL` — a company already carrying an ecosystem kind (e.g. a
        cloud provider that also happens to be someone's client) is never touched;
        genuine dual-role companies keep their ecosystem kind.
      - `website IS NULL` and no `TAGGED_AS` topic — an already-researched company
        is out of scope.
      - **no outgoing edges at all** (`outDeg = 0`): no clients of its own, no
        outbound partnership, no topic/company-type tag, no CITES provenance.
      - **the only incoming edges are HAS_CLIENT** (`inOther = 0`): rules out an
        inbound partnership and any LEADS from a person.
      - at least one inbound HAS_CLIENT (`EXISTS`), so it really is someone's client.

    Purely additive to the graph read layer; like the rest of it, it skips in CI
    when Neo4j is absent. Nothing is written — the caller reviews and commits.
    """
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Company)
            WHERE c.kind IS NULL
              AND c.website IS NULL
              AND NOT coalesce(c.junk, false)
              AND NOT (c)-[:TAGGED_AS]->(:Topic)
              AND EXISTS { (:Company)-[:HAS_CLIENT]->(c) }
            OPTIONAL MATCH (c)-[out]->()
            WITH c, count(out) AS outDeg
            OPTIONAL MATCH (c)<-[inc]-()
            WITH c, outDeg, sum(CASE WHEN type(inc) = 'HAS_CLIENT' THEN 0 ELSE 1 END) AS inOther,
                 sum(CASE WHEN type(inc) = 'HAS_CLIENT' THEN 1 ELSE 0 END) AS inbound
            WHERE outDeg = 0 AND inOther = 0
            RETURN c.name AS name, inbound
            ORDER BY inbound DESC, name
            """
        )
        return [dict(record) async for record in result]


async def remove_stub_companies(
    driver: AsyncDriver, names: list[str]
) -> tuple[list[str], list[str]]:
    """HARD-DELETE true stubs by name; REFUSE researched companies. IRREVERSIBLE.

    A "true stub" is a node with no website and no `TAGGED_AS` topic — the same
    not-yet-researched shape the merge TOCTOU guard protects (see `_merge_tx`).
    A node that carries a website or a topic tag has been researched: deleting it
    would destroy real data, so it is refused (skipped) rather than removed, even
    if the reviewer approved it (a stub may have been promoted while review sat
    open). Only the human-in-the-loop classification commit calls this, and only
    for reviewer-approved 'remove' decisions.

    Returns `(removed_names, refused_names)`. Unknown names simply don't match and
    appear in neither list. Idempotent: a second call removes nothing new.
    """
    if not names:
        return [], []
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Company) WHERE c.name IN $names
            OPTIONAL MATCH (c)-[t:TAGGED_AS]->(:Topic)
            WITH c, (c.website IS NULL AND count(t) = 0) AS isStub
            WITH collect(CASE WHEN isStub THEN c.name END) AS removed,
                 collect(CASE WHEN NOT isStub THEN c.name END) AS refused,
                 collect(CASE WHEN isStub THEN c END) AS doomed
            FOREACH (d IN doomed | DETACH DELETE d)
            RETURN removed, refused
            """,
            names=names,
        )
        record = await result.single()
    if record is None:
        return [], []
    return list(record["removed"]), list(record["refused"])


async def classify_stub_kinds(
    driver: AsyncDriver, kind_writes: list[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    """Set each approved kind, guarded by the FULL scan predicate at commit time.

    The reviewer's decision was made about a STUB; if the node gained any signal
    while review sat open (a kind, a website, a topic tag, a junk flag, an
    outbound edge, or a non-HAS_CLIENT inbound edge), the premise of that
    decision is stale — writing the stale kind would silently clobber real
    research (PR #188 review; the guard `classify_as_client` carried before the
    per-decision refactor). Such names are REFUSED rather than written; a fresh
    scan re-proposes against current state.

    Returns `(classified_names, refused_names)`. Unknown names match nothing and
    appear in neither list.
    """
    if not kind_writes:
        return [], []
    pairs = [{"name": n, "kind": k} for n, k in kind_writes]
    async with driver.session() as session:
        result = await session.run(
            """
            UNWIND $pairs AS pair
            MATCH (c:Company {name: pair.name})
              WHERE c.kind IS NULL
              AND c.website IS NULL
              AND NOT coalesce(c.junk, false)
              AND NOT (c)-[:TAGGED_AS]->(:Topic)
              AND EXISTS { (:Company)-[:HAS_CLIENT]->(c) }
            OPTIONAL MATCH (c)-[out]->()
            WITH c, pair, count(out) AS outDeg
            OPTIONAL MATCH (c)<-[inc]-()
            WITH c, pair, outDeg,
                 sum(CASE WHEN type(inc) = 'HAS_CLIENT' THEN 0 ELSE 1 END) AS inOther
            WHERE outDeg = 0 AND inOther = 0
            SET c.kind = pair.kind, c.updatedAt = datetime()
            RETURN collect(c.name) AS classified
            """,
            pairs=pairs,
        )
        record = await result.single()
    classified = list(record["classified"]) if record else []
    refused = [n for n, _ in kind_writes if n not in classified]
    return classified, refused


async def flag_junk(driver: AsyncDriver, names: list[str]) -> int:
    """Mark companies as junk so they drop out of the company/backlog lists.

    Idempotent; silently ignores names with no matching node. Returns the count
    of nodes actually flagged.
    """
    if not names:
        return 0
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Company) WHERE c.name IN $names
            SET c.junk = true, c.updatedAt = datetime()
            RETURN count(c) AS flagged
            """,
            names=names,
        )
        record = await result.single()
    return record["flagged"] if record else 0


async def add_aliases(driver: AsyncDriver, canonical: str, aliases: list[str]) -> list[str]:
    """Record extra spellings on a canonical node (no node deleted).

    Future enrichment MERGEs resolve these aliases back to the canonical (see
    `repository.upsert_company`). The canonical's own name is never stored as an
    alias. Returns the node's alias list after the update (empty if unknown).
    """
    clean = sorted({a.strip() for a in aliases if a.strip() and a.strip() != canonical})
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Company {name: $canonical})
            WITH c, [a IN $aliases WHERE NOT a IN coalesce(c.aliases, [])] AS additions
            SET c.aliases = coalesce(c.aliases, []) + additions, c.updatedAt = datetime()
            RETURN c.aliases AS aliases
            """,
            canonical=canonical,
            aliases=clean,
        )
        record = await result.single()
    return record["aliases"] if record else []


# Org-to-org and provenance edges re-pointed on merge. HAS_CLIENT/PARTNERS_WITH
# can point either way (a stub may be someone's client and have clients of its
# own); CITES/LEADS attach in one direction. Kept explicit rather than via APOC
# so it runs on a stock Neo4j (local Docker and Aura alike).
async def merge_companies(
    driver: AsyncDriver,
    canonical: str,
    variants: list[str],
    *,
    allow_researched: bool = False,
) -> dict:
    """Merge variant company nodes into `canonical`. IRREVERSIBLE — commit only.

    For each variant: re-point its HAS_CLIENT / PARTNERS_WITH / CITES / LEADS
    edges onto the canonical (dropping edges that would become self-loops),
    union the variant's scalar properties into gaps on the canonical, fold the
    variant's name + its own aliases into `canonical.aliases`, then delete the
    variant.

    Idempotent-safe and defensive: a variant equal to the canonical, a
    self-merge, an already-merged (missing) variant, or an unknown canonical are
    all skipped rather than erroring. Returns a summary of what happened.

    `allow_researched` relaxes the promoted-variant TOCTOU guard (see `_merge_tx`).
    It stays False for the scan flow — there a "stub" that gained a website/topic
    while review sat open was never meant to be merged, so deleting it would be an
    accident. It is set True ONLY for an explicit, user-named merge (the scoped
    chat flow), where the user asserted these are the same organisation and a
    researched variant is intended, not accidental. The union is still
    non-destructive to the survivor (edges/provenance re-pointed, props fill gaps
    only), so the researched canonical keeps its data.
    """
    if not canonical:
        return {
            "canonical": canonical,
            "merged": [],
            "skipped": list(variants),
            "error": "no canonical",
        }
    targets = [v for v in dict.fromkeys(variants) if v and v != canonical]
    async with driver.session() as session:
        return await session.execute_write(_merge_tx, canonical, targets, allow_researched)


async def _merge_tx(
    tx: AsyncManagedTransaction,
    canonical: str,
    variants: list[str],
    allow_researched: bool = False,
) -> dict:
    # Bail cleanly if the survivor doesn't exist — don't create it implicitly.
    exists = await tx.run("MATCH (c:Company {name: $name}) RETURN c.name", name=canonical)
    if await exists.single() is None:
        return {
            "canonical": canonical,
            "merged": [],
            "skipped": variants,
            "error": "unknown canonical",
        }

    merged: list[str] = []
    skipped: list[str] = []
    promoted: list[str] = []  # variants no longer stubs at commit time (TOCTOU guard)
    for variant in variants:
        # Read both property maps up front. Computing the property union in
        # Python (rather than a dynamic-key `SET canon[k] = v[k]`, which needs a
        # recent Neo4j / APOC) keeps the merge portable across DB versions.
        props = await tx.run(
            "MATCH (v:Company {name: $variant}) "
            "OPTIONAL MATCH (canon:Company {name: $canonical}) "
            "RETURN properties(v) AS vp, properties(canon) AS cp, "
            "       (EXISTS { (v)-[:TAGGED_AS]->(:Topic) } OR v.website IS NOT NULL) AS promoted",
            canonical=canonical,
            variant=variant,
        )
        row = await props.single()
        if row is None or row["vp"] is None:
            skipped.append(variant)  # already merged / never existed — no-op
            continue
        if row["promoted"] and not allow_researched:
            # TOCTOU guard: review can sit open, and an enrichment in the meantime
            # may have promoted this "stub" to a researched company (topic tag or
            # website). Deleting it now would destroy researched data — skip it;
            # a fresh scan can re-propose against current state. Relaxed only for
            # an explicit, user-named merge (allow_researched), where a researched
            # variant is intended and the union still preserves the survivor.
            skipped.append(variant)
            promoted.append(variant)
            continue
        variant_props, canon_props = row["vp"], row["cp"]

        # Fill only the canonical's gaps — never overwrite a value it already
        # has — and skip identity/bookkeeping keys.
        skip_keys = {"name", "aliases", "junk", "updatedAt"}
        fill = {
            k: val
            for k, val in variant_props.items()
            if k not in skip_keys and canon_props.get(k) is None
        }
        existing_aliases = list(canon_props.get("aliases") or [])
        for alias in list(variant_props.get("aliases") or []) + [variant]:
            if alias != canonical and alias not in existing_aliases:
                existing_aliases.append(alias)

        await tx.run(
            "MATCH (canon:Company {name: $canonical}) "
            "SET canon += $fill, canon.aliases = $aliases, canon.updatedAt = datetime()",
            canonical=canonical,
            fill=fill,
            aliases=existing_aliases,
        )

        # Re-point simple (property-less) edges. MERGE de-dupes; the self-loop
        # guard drops edges between the variant and the canonical itself.
        for rel, direction in (
            ("HAS_CLIENT", "out"),
            ("HAS_CLIENT", "in"),
            ("PARTNERS_WITH", "out"),
            ("PARTNERS_WITH", "in"),
        ):
            if direction == "out":
                pattern = f"(v)-[r:{rel}]->(other)"
                new_pattern = f"(canon)-[:{rel}]->(other)"
            else:
                pattern = f"(other)-[r:{rel}]->(v)"
                new_pattern = f"(other)-[:{rel}]->(canon)"
            await tx.run(
                f"""
                MATCH (canon:Company {{name: $canonical}}), (v:Company {{name: $variant}})
                MATCH {pattern}
                WHERE other <> canon
                MERGE {new_pattern}
                DELETE r
                """,
                canonical=canonical,
                variant=variant,
            )

        # LEADS carries a `title` on the relationship — copy it so a re-pointed
        # leader keeps their role (coalesce keeps any title canon already had).
        await tx.run(
            """
            MATCH (canon:Company {name: $canonical})
            MATCH (person:Person)-[r:LEADS]->(v:Company {name: $variant})
            MERGE (person)-[nr:LEADS]->(canon)
            SET nr.title = coalesce(nr.title, r.title)
            DELETE r
            """,
            canonical=canonical,
            variant=variant,
        )

        # CITES carries provenance properties (field/value/sourceDate/capturedAt)
        # — copy them onto the re-pointed edge so a figure stays traceable.
        await tx.run(
            """
            MATCH (canon:Company {name: $canonical}), (v:Company {name: $variant})
            MATCH (v)-[r:CITES]->(s:Source)
            MERGE (canon)-[nr:CITES {field: r.field}]->(s)
            SET nr.value = r.value, nr.sourceDate = r.sourceDate,
                nr.capturedAt = coalesce(r.capturedAt, datetime())
            DELETE r
            """,
            canonical=canonical,
            variant=variant,
        )

        # The variant is now stripped of edges — remove it.
        await tx.run("MATCH (v:Company {name: $name}) DETACH DELETE v", name=variant)
        merged.append(variant)

    return {"canonical": canonical, "merged": merged, "skipped": skipped, "promoted": promoted}

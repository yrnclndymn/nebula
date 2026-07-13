"""Entity resolution: detection heuristics (pure, no DB) + merge graph surgery
(skips without Neo4j, like the rest of the graph layer). Fictional names only.
"""

import asyncio

import pytest

from app.graph import entity_resolution as er
from app.graph.driver import check_connectivity, close_driver, get_driver
from app.graph.schema import apply_schema

# --- Normalisation / legal-suffix stripping (pure) ---------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Acme, LLC", "acme"),
        ("Acme Inc", "acme"),
        ("Acme Ltd.", "acme"),
        ("The Globex Company", "globex"),
        ("Globex GmbH", "globex"),
        ("Acme & Sons", "acme sons"),
        ("Initech Corporation", "initech"),
        ("  Umbrella   Corp  ", "umbrella"),
    ],
)
def test_normalize_strips_suffixes_and_noise(name, expected):
    assert er.normalize_name(name) == expected


def test_normalize_keeps_descriptive_words():
    # "Bank" / "Labs" are descriptive, not legal forms — they must survive.
    assert er.normalize_name("Acme Bank Inc") == "acme bank"
    assert er.normalize_name("Globex Labs LLC") == "globex labs"


def test_normalize_all_legal_tokens_does_not_vanish():
    # A name made only of legal/stop tokens keeps something rather than "".
    assert er.normalize_name("The Company") != ""


# --- Variant cluster detection (pure) ----------------------------------------


def test_detects_legal_suffix_variants():
    clusters = er.detect_variant_clusters(["Acme", "Acme Inc", "Acme, LLC", "Globex"])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["reason"] == "normalized"
    assert cluster["members"] == ["Acme", "Acme Inc", "Acme, LLC"]
    assert cluster["canonical"] in cluster["members"]


def test_detects_containment_variant():
    clusters = er.detect_variant_clusters(["Initech", "Initech Digital"])
    assert len(clusters) == 1
    assert clusters[0]["reason"] == "containment"
    assert set(clusters[0]["members"]) == {"Initech", "Initech Digital"}


def test_short_token_does_not_chain_unrelated_names():
    # "AB" is < 4 chars normalised, so it must NOT glue "AB Systems" to "AB Foods".
    clusters = er.detect_variant_clusters(["AB", "AB Systems", "AB Foods"])
    joined = {frozenset(c["members"]) for c in clusters}
    assert frozenset({"AB Systems", "AB Foods"}) not in joined


def test_distinct_companies_are_not_clustered():
    assert er.detect_variant_clusters(["Acme", "Globex", "Initech", "Umbrella"]) == []


def test_generic_leading_token_does_not_chain_distinct_orgs():
    # Regression (issue #67), fictionalised from an observed prod false positive:
    # several clearly-distinct orgs share a single GENERIC geographic token
    # ("Central …") and a bare stub of that token sat alongside them. Single-token
    # containment then chained the bare stub to every "Central X", collapsing
    # unrelated health/logistics/food bodies into one proposed cluster. A lone
    # generic token must never bridge them.
    names = ["Central", "Central Health", "Central Logistics", "Central Foods"]
    clusters = er.detect_variant_clusters(names)
    # No pair of the distinct "Central X" orgs may share a cluster...
    for c in clusters:
        members = set(c["members"])
        assert not ({"Central Health", "Central Logistics"} <= members)
        assert not ({"Central Health", "Central Foods"} <= members)
        assert not ({"Central Logistics", "Central Foods"} <= members)
    # ...and with nothing else linking them, no multi-org cluster is proposed.
    assert clusters == []


def test_distinctive_token_still_clusters_despite_generic_partner():
    # The tightening must not over-correct: a DISTINCTIVE single token still
    # anchors a containment merge even when the extra token is generic ("Health").
    clusters = er.detect_variant_clusters(["Globex", "Globex Health"])
    assert len(clusters) == 1
    assert set(clusters[0]["members"]) == {"Globex", "Globex Health"}
    assert clusters[0]["reason"] == "containment"


def test_two_shared_tokens_cluster_even_if_both_generic():
    # >= 2 shared tokens is strong enough on its own (acceptance), so an exact
    # prefix of a longer name still clusters even when every token is generic.
    clusters = er.detect_variant_clusters(["Central Health", "Central Health Group"])
    assert len(clusters) == 1
    assert set(clusters[0]["members"]) == {"Central Health", "Central Health Group"}


def test_canonical_prefers_most_descriptive():
    clusters = er.detect_variant_clusters(["Acme", "Acme Digital Partners"])
    # Most normalised tokens wins as the default survivor.
    assert clusters[0]["canonical"] == "Acme Digital Partners"


def test_singletons_are_dropped():
    assert er.detect_variant_clusters(["Acme"]) == []


# --- Junk heuristic (pure) ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Read more", "Our Clients", "click here", "© 2026", "   ", "12345", "Privacy Policy"],
)
def test_junk_flags_noise(name):
    assert er.looks_like_junk(name) is True


@pytest.mark.parametrize("name", ["Acme", "Globex Labs", "Initech", "Hooli"])
def test_junk_keeps_real_names(name):
    assert er.looks_like_junk(name) is False


# --- Merge graph surgery (needs Neo4j) ---------------------------------------

CANON = "Acme __ertest__"
VARIANT = "Acme Inc __ertest__"
CLIENT_OF = "Globex __ertest__"
PARTNER = "Initech __ertest__"
SOURCE_URL = "https://example.invalid/ertest"


def test_merge_repoints_edges_unions_props_and_aliases():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        await apply_schema(d)
        async with d.session() as s:
            # Canonical is a bare stub; the variant carries the edges + a prop.
            # A partner points AT the variant (incoming edge) to exercise both
            # directions and the self-loop guard.
            await s.run(
                """
                MERGE (canon:Company {name:$canon})
                MERGE (v:Company {name:$variant}) SET v.headcount = 7
                MERGE (client:Company {name:$client})
                MERGE (partner:Company {name:$partner})
                MERGE (src:Source {url:$src})
                MERGE (v)-[:HAS_CLIENT]->(client)
                MERGE (partner)-[:PARTNERS_WITH]->(v)
                MERGE (canon)-[:PARTNERS_WITH]->(v)
                MERGE (v)-[r:CITES {field:'headcount'}]->(src)
                  SET r.value = '7'
                """,
                canon=CANON,
                variant=VARIANT,
                client=CLIENT_OF,
                partner=PARTNER,
                src=SOURCE_URL,
            )

        result = await er.merge_companies(d, CANON, [VARIANT, CANON, "Nope __ertest__"])

        async with d.session() as s:
            row = await (
                await s.run(
                    """
                    MATCH (canon:Company {name:$canon})
                    RETURN canon.headcount AS headcount,
                           canon.aliases AS aliases,
                           EXISTS { (canon)-[:HAS_CLIENT]->(:Company {name:$client}) } AS hasClient,
                           EXISTS { (:Company {name:$partner})-[:PARTNERS_WITH]->(canon) } AS hasPartner,
                           EXISTS { (canon)-[:CITES]->(:Source {url:$src}) } AS hasCite,
                           EXISTS { (canon)-[:PARTNERS_WITH]->(canon) } AS selfLoop,
                           EXISTS { (:Company {name:$variant}) } AS variantLives
                    """,
                    canon=CANON,
                    variant=VARIANT,
                    client=CLIENT_OF,
                    partner=PARTNER,
                    src=SOURCE_URL,
                )
            ).single()
        async with d.session() as s:
            await s.run(
                "MATCH (c:Company) WHERE c.name IN $names DETACH DELETE c",
                names=[CANON, VARIANT, CLIENT_OF, PARTNER],
            )
            await s.run("MATCH (src:Source {url:$src}) DETACH DELETE src", src=SOURCE_URL)
        await close_driver()
        return result, row

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    result, row = out
    assert result["merged"] == [VARIANT]  # canonical + unknown skipped, not errored
    assert VARIANT in result["skipped"] or "Nope __ertest__" in result["skipped"]
    assert row["variantLives"] is False  # variant node deleted
    assert row["headcount"] == 7  # scalar unioned into the canonical's gap
    assert VARIANT in row["aliases"]  # variant name recorded as an alias
    assert row["hasClient"] is True  # outgoing edge re-pointed
    assert row["hasPartner"] is True  # incoming edge re-pointed
    assert row["hasCite"] is True  # provenance re-pointed
    assert row["selfLoop"] is False  # variant↔canonical edge dropped, not looped


def test_merge_unknown_canonical_is_safe():
    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        result = await er.merge_companies(d, "Ghost __ertest__", ["Whatever __ertest__"])
        await close_driver()
        return result

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    assert out["merged"] == []
    assert "error" in out


def test_merge_skips_variant_promoted_since_scan():
    """TOCTOU guard: a variant that gained a topic tag or website between scan
    and commit is no longer a stub — merging would delete researched data, so it
    must be skipped (reported under both skipped and promoted)."""
    canon = "Umbrella __ertest__"
    promoted_v = "Umbrella Inc __ertest__"

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        async with d.session() as s:
            await s.run("MATCH (n) WHERE n.name IN [$a,$b] DETACH DELETE n", a=canon, b=promoted_v)
            await s.run(
                """
                MERGE (canon:Company {name:$canon})
                MERGE (v:Company {name:$v}) SET v.website = 'https://example.invalid'
                """,
                canon=canon,
                v=promoted_v,
            )
        result = await er.merge_companies(d, canon, [promoted_v])
        async with d.session() as s:
            r = await s.run("MATCH (v:Company {name:$v}) RETURN count(v) AS n", v=promoted_v)
            survives = (await r.single())["n"]
            await s.run("MATCH (n) WHERE n.name IN [$a,$b] DETACH DELETE n", a=canon, b=promoted_v)
        await close_driver()
        return result, survives

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    result, survives = out
    assert result["merged"] == []
    assert result["promoted"] == [promoted_v]
    assert promoted_v in result["skipped"]
    assert survives == 1  # the promoted node was NOT deleted


def test_flag_junk_marks_and_excludes():
    """flag_junk sets the junk flag (idempotently) and unknown names are ignored."""

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        name = "Read More __ertest__"
        async with d.session() as s:
            await s.run("MATCH (c:Company {name:$n}) DETACH DELETE c", n=name)
            await s.run("CREATE (:Company {name:$n})", n=name)
        first = await er.flag_junk(d, [name, "No Such Co __ertest__"])
        second = await er.flag_junk(d, [name])  # idempotent re-flag
        async with d.session() as s:
            r = await s.run("MATCH (c:Company {name:$n}) RETURN c.junk AS junk", n=name)
            junk = (await r.single())["junk"]
            await s.run("MATCH (c:Company {name:$n}) DETACH DELETE c", n=name)
        await close_driver()
        return first, second, junk

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    first, second, junk = out
    assert first == 1  # only the existing node counted; unknown name ignored
    assert second == 1
    assert junk is True


def test_add_aliases_appends_without_duplicates():
    """add_aliases records new spellings, never duplicates, never the canonical
    itself, and returns [] for an unknown canonical."""

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        canon = "Hooli __ertest__"
        async with d.session() as s:
            await s.run("MATCH (c:Company {name:$n}) DETACH DELETE c", n=canon)
            await s.run("CREATE (:Company {name:$n})", n=canon)
        first = await er.add_aliases(d, canon, ["Hooli Inc __ertest__", canon, "  "])
        second = await er.add_aliases(d, canon, ["Hooli Inc __ertest__", "Hooli LLC __ertest__"])
        unknown = await er.add_aliases(d, "No Such Co __ertest__", ["X"])
        async with d.session() as s:
            await s.run("MATCH (c:Company {name:$n}) DETACH DELETE c", n=canon)
        await close_driver()
        return first, second, unknown

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    first, second, unknown = out
    assert first == ["Hooli Inc __ertest__"]  # canonical + blank filtered out
    assert sorted(second) == ["Hooli Inc __ertest__", "Hooli LLC __ertest__"]  # no dupes
    assert unknown == []

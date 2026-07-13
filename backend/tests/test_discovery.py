"""Web discovery (issue #75): profile facts, query generation, candidate
extraction, dedup, the durable job flow, and the "research selected" trigger.

The heuristics are pure — no DB, no model, no network — so they run anywhere. The
full job flow is graph-gated (skips without Neo4j; CI is the arbiter). Fictional
company names only (the repo is public).
"""

import asyncio

import pytest

from app.agents.discovery.dedup import filter_new, is_known
from app.agents.discovery.extract import candidate_name, extract_candidates, official_domain
from app.agents.discovery.hostpick import (
    best_host,
    domain_label,
    name_domain_similarity,
    page_mentions_name,
    rank_hosts,
)
from app.agents.discovery.profile import CohortProfile, derive_profile_facts
from app.agents.discovery.search import build_queries

# --- Profile facts (pure) -----------------------------------------------------


def test_profile_facts_dominant_kind_country_and_topics():
    seed = {
        "name": "Acme",
        "kind": "service_provider",
        "hqCountry": "United Kingdom",
        "topics": ["SAP", "Cloud"],
    }
    cohort = [
        {
            "name": "Globex",
            "kind": "service_provider",
            "hqCountry": "United Kingdom",
            "topics": ["SAP"],
        },
        {"name": "Initech", "kind": "isv", "hqCountry": "Germany", "topics": ["SAP", "Data"]},
    ]
    p = derive_profile_facts(seed, cohort)
    assert p.seed == "Acme"
    assert p.kind == "service_provider"  # 2 of 3 win
    assert p.country == "United Kingdom"  # 2 of 3 win
    assert p.topics[0] == "SAP"  # most frequent (3)
    assert p.cohort == ["Globex", "Initech"]
    # Terms are lowercase, topic-led, and include the country; "company" filler out.
    assert "sap" in p.terms
    assert "united kingdom" in p.terms
    assert "company" not in p.terms


def test_profile_facts_handles_empty_fields():
    seed = {"name": "Acme", "kind": None, "hqCountry": None, "topics": []}
    p = derive_profile_facts(seed, [])
    assert p.kind is None and p.country is None
    assert p.topics == [] and p.cohort == []


def test_build_profile_survives_summary_failure(monkeypatch):
    """A summariser exception (persistent quota, safety block) must not fail the
    profile — query generation runs on the structured facts alone (#83 review)."""
    from app.agents.discovery import profile as profile_mod

    async def fake_rows(driver, names):
        return [
            {"name": "Acme", "kind": "isv", "hqCountry": "Sweden", "topics": ["SAP"]},
            {"name": "Globex", "kind": "isv", "hqCountry": "Sweden", "topics": ["SAP"]},
        ]

    async def boom(seed, rows):
        raise RuntimeError("simulated persistent 429")

    monkeypatch.setattr(profile_mod.queries, "cohort_profile_rows", fake_rows)
    monkeypatch.setattr(profile_mod, "summarise_cohort", boom)
    p = asyncio.run(profile_mod.build_profile(None, "Acme", ["Globex"]))
    assert p.summary == ""  # degraded, not dead
    assert p.kind == "isv" and p.cohort == ["Globex"]


# --- Query generation (pure) --------------------------------------------------


def test_build_queries_triangulates_and_caps():
    p = CohortProfile(
        seed="Acme",
        kind="service_provider",
        country="United Kingdom",
        topics=["SAP", "Cloud"],
        cohort=["Globex"],
        terms=["sap", "cloud"],
    )
    qs = build_queries(p)
    assert any("United Kingdom" in q for q in qs)  # category + geography
    assert "Acme competitors" in qs  # seed-relative
    assert any(q.startswith("companies like Acme and Globex") for q in qs)  # peer-relative
    assert len(qs) <= 5
    assert len(qs) == len(set(q.lower() for q in qs))  # de-duplicated


def test_build_queries_without_country_or_cohort():
    p = CohortProfile(seed="Acme", kind="isv", country=None, topics=[], cohort=[], terms=[])
    qs = build_queries(p)
    assert "Acme competitors" in qs
    assert "companies like Acme" in qs  # no peer to append
    assert all("None" not in q for q in qs)


# --- Candidate extraction (pure) ----------------------------------------------


def test_official_domain_skips_social_and_directories():
    assert official_domain("https://www.globex.example/about") == "globex.example"
    assert official_domain("https://linkedin.com/company/globex") is None
    assert official_domain("https://crunchbase.com/organization/globex") is None
    assert official_domain("") is None


def test_candidate_name_takes_leading_segment():
    assert candidate_name("Globex Consulting - SAP experts") == "Globex Consulting"
    assert candidate_name("Initech | Cloud data") == "Initech"
    assert candidate_name("A very long sentence that is clearly not a company name at all") == ""


def test_extract_candidates_dedupes_by_domain_and_unions_evidence():
    results = [
        {
            "title": "Globex Consulting - SAP experts",
            "url": "https://globex.example/",
            "snippet": "SAP consultancy",
        },
        {
            "title": "Globex on LinkedIn",
            "url": "https://linkedin.com/company/globex",
            "snippet": "profile",
        },
        {
            "title": "Globex Consulting - Cloud",
            "url": "https://globex.example/cloud",
            "snippet": "cloud too",
        },
        {
            "title": "Initech | Cloud data",
            "url": "https://initech.example",
            "snippet": "cloud data",
        },
    ]
    cands = extract_candidates(results, terms=["sap", "cloud"])
    by_name = {c["name"]: c for c in cands}
    assert set(by_name) == {"Globex Consulting", "Initech"}  # LinkedIn dropped
    globex = by_name["Globex Consulting"]
    assert globex["website"] == "globex.example"
    assert len(globex["sources"]) == 2  # both globex results merged
    assert set(globex["why"]) == {"sap", "cloud"}  # evidence unioned across results


def test_extract_candidates_excludes_domains():
    results = [{"title": "Acme Corp", "url": "https://acme.example", "snippet": "x"}]
    assert extract_candidates(results, terms=[], exclude_domains={"acme.example"}) == []


def test_extract_candidates_drops_non_http_urls():
    """`sources` is rendered as links in the review UI — a hostile result with a
    javascript:/data: scheme (or anything schemeless-weird) must never survive."""
    results = [
        {"title": "Evil Co", "url": "javascript:alert(1)", "snippet": "x"},
        {"title": "Evil Data", "url": "data:text/html,<script>1</script>", "snippet": "x"},
        {"title": "Fine Co", "url": "https://fine.example", "snippet": "x"},
    ]
    cands = extract_candidates(results, terms=[])
    assert [c["name"] for c in cands] == ["Fine Co"]
    assert all(s.startswith("https://") for c in cands for s in c["sources"])


# --- Host choice: name <-> domain similarity (pure, issue #67) -----------------


def test_domain_label_strips_www_tld_and_ccsld():
    assert domain_label("https://www.acme.com") == "acme"
    assert domain_label("acme.co.uk") == "acme"
    # A subdomain resolves to the registrable label, not the leaf.
    assert domain_label("https://foundation.acme.org/about") == "acme"
    assert domain_label("") == ""


def test_name_domain_similarity_rewards_resemblance():
    # An exact name<->domain match beats a longer host that merely shares a prefix,
    # which in turn beats an unrelated host.
    exact = name_domain_similarity("Nimbus Lab", "nimbuslab.ai")
    prefix = name_domain_similarity("Nimbus Lab", "nimbusfoundation.org")
    unrelated = name_domain_similarity("Nimbus Lab", "getwidgets.io")
    assert exact == 1.0
    assert exact > prefix > unrelated
    assert unrelated < 0.5


def test_best_host_prefers_name_matching_domain_over_search_order():
    # Regression (issue #67), fictionalised from an observed prod false positive:
    # searching a lab's official site, a *foundation* on a DIFFERENT domain ranks
    # first in the results; the lab's own domain (an exact name match) comes later.
    # Similarity ranking must pick the lab's own host, not the first search hit.
    name = "Nimbus Lab"
    results = [
        {"title": "Nimbus Foundation - charity", "url": "https://nimbusfoundation.org/"},
        {"title": "Nimbus Lab", "url": "https://nimbuslab.ai/"},
    ]
    assert best_host(name, results) == "nimbuslab.ai"


def test_best_host_falls_back_to_search_order_when_nothing_resembles():
    # A rebranded company whose site shares no tokens with its name: no host
    # resembles it, so the pick degrades to the first non-blocklisted result
    # (the legacy behaviour), NOT some spuriously-ranked host.
    name = "Acme"
    results = [
        {"title": "Acme - Home", "url": "https://getwidgets.io/"},
        {"title": "Acme on Crunchbase", "url": "https://crunchbase.com/acme"},
        {"title": "Acme", "url": "https://madeby.dev/"},
    ]
    assert best_host(name, results) == "getwidgets.io"


def test_rank_hosts_skips_blocklisted_and_bad_schemes():
    name = "Globex"
    results = [
        {"title": "Globex on LinkedIn", "url": "https://linkedin.com/company/globex"},
        {"title": "Evil", "url": "javascript:alert(1)"},
        {"title": "Globex", "url": "https://globex.example/"},
    ]
    ranked = rank_hosts(name, results)
    assert [r.host for r in ranked] == ["globex.example"]


def test_best_host_none_when_no_official_candidate():
    results = [{"title": "X", "url": "https://linkedin.com/company/x"}]
    assert best_host("X", results) is None


def test_page_mentions_name_is_token_and_case_insensitive():
    assert page_mentions_name("Nimbus Lab", "Welcome to Nimbus Lab, an AI research group.")
    assert page_mentions_name("Nimbus Lab", "NIMBUS  LAB — home")  # case + spacing
    # A landing page for a different org must NOT count as a mention.
    assert not page_mentions_name("Nimbus Lab", "This is the Aardvark Foundation site.")
    # A token appearing only inside a larger word is not a mention.
    assert not page_mentions_name("Lab Co", "We run an elaborate collaboration.")


# --- Dedup against the graph (pure matcher) -----------------------------------


def test_is_known_matches_domain_or_name_key():
    name_keys = {"globex"}  # normalize_name("Globex Inc") == "globex"
    domains = {"initech.example"}
    assert is_known({"name": "Globex Inc", "website": "other.example"}, name_keys, domains)
    assert is_known({"name": "Something", "website": "https://initech.example"}, name_keys, domains)
    assert not is_known({"name": "Newco", "website": "newco.example"}, name_keys, domains)


def test_filter_new_drops_known_and_self_dupes():
    name_keys = {"globex"}
    domains = {"initech.example"}
    candidates = [
        {"name": "Globex Inc", "website": "other.example", "why": [], "sources": []},
        {"name": "Initech", "website": "initech.example", "why": [], "sources": []},
        {"name": "Newco", "website": "newco.example", "why": [], "sources": []},
        {
            "name": "Newco Ltd",
            "website": "newco-uk.example",
            "why": [],
            "sources": [],
        },  # same key as Newco
    ]
    new = filter_new(candidates, name_keys, domains)
    assert [c["name"] for c in new] == ["Newco"]  # known dropped, second Newco collapsed


# --- "Research selected" trigger (mocked; no DB, no ADK) ----------------------


def test_research_candidates_caps_filters_and_staggers(monkeypatch):
    from app.agents.assistant import proposals
    from app.agents.discovery import discovery

    job = {"candidates": [{"name": f"Cand {i}", "website": f"c{i}.example"} for i in range(12)]}

    async def fake_get_job(job_id):
        return job

    monkeypatch.setattr(discovery.jobs, "get_job", fake_get_job)

    calls = []

    async def fake_propose(name, website="", topic="", focus="", enqueue_delay=0.0):
        calls.append((name, website, enqueue_delay))
        return {"proposal_id": f"p{len(calls)}", "name": name, "status": "pending"}

    monkeypatch.setattr(proposals, "propose_enrichment", fake_propose)
    monkeypatch.setattr(discovery.settings, "research_stagger_seconds", 5.0)

    names = [f"Cand {i}" for i in range(12)] + ["Not In Job"]
    res = asyncio.run(discovery.research_candidates("job1", names))

    assert len(res["proposals"]) == 10  # capped at MAX_DISCOVERY_RESEARCH
    assert res["cap"] == 10
    assert calls[0] == ("Cand 0", "c0.example", 0.0)  # discovered website passed through
    assert [c[2] for c in calls[:3]] == [0.0, 5.0, 10.0]  # staggered
    assert all(name != "Not In Job" for name, _, _ in calls)  # only in-job names accepted


def test_research_candidates_rejects_unknown_job(monkeypatch):
    from app.agents.discovery import discovery

    async def fake_get_job(job_id):
        return None

    monkeypatch.setattr(discovery.jobs, "get_job", fake_get_job)
    res = asyncio.run(discovery.research_candidates("nope", ["Anything"]))
    assert "error" in res


# --- Full job flow (graph-gated; profile + search mocked) ---------------------


def test_run_discovery_job_end_to_end(monkeypatch):
    from app.agents.discovery import discovery
    from app.agents.discovery.profile import CohortProfile
    from app.graph import jobs
    from app.graph.driver import check_connectivity, close_driver, get_driver

    async def scenario():
        try:
            await check_connectivity()
        except Exception:
            return "skip"
        d = get_driver()
        # A company already in the graph — dedup must drop it by domain.
        async with d.session() as s:
            await s.run(
                "MERGE (c:Company {name:$n}) SET c.website=$w",
                n="__disc_known__",
                w="https://known.example",
            )
        job_id = "__disctest__"
        await jobs.create_job(
            job_id,
            "discovery",
            {
                "job_id": job_id,
                "status": "pending",
                "name": "__disc_seed__",
                "seed": "__disc_seed__",
                "cohort": [],
                "queries": [],
                "candidates": [],
            },
        )

        async def fake_profile(driver, seed, cohort):
            return CohortProfile(
                seed="__disc_seed__",
                kind="service_provider",
                country="United Kingdom",
                topics=["sap"],
                cohort=[],
                terms=["sap"],
                summary="a group of SAP consultancies",
            )

        def fake_search(query):
            return {
                "results": [
                    {"title": "Newco - SAP", "url": "https://newco.example", "snippet": "sap"},
                    {"title": "Known - SAP", "url": "https://known.example", "snippet": "sap"},
                ]
            }

        monkeypatch.setattr(discovery, "build_profile", fake_profile)
        monkeypatch.setattr(discovery, "web_search", fake_search)

        await discovery.run_discovery_job(job_id)
        job = await jobs.get_job(job_id)

        await jobs.delete_job(job_id)
        async with d.session() as s:
            await s.run("MATCH (c:Company {name:$n}) DETACH DELETE c", n="__disc_known__")
        await close_driver()
        return job

    out = asyncio.run(scenario())
    if out == "skip":
        pytest.skip("Neo4j not reachable — run `make db-up`")
    assert out["status"] == "ready"
    assert out["queries"]  # queries were generated
    names = {c["name"] for c in out["candidates"]}
    assert "Newco" in names  # a genuinely new company survives
    assert "Known" not in names  # the already-captured one deduped by domain
    assert "found" in out["outcome"]


# --- discover_website wiring (async; search + fetch mocked) --------------------
# The pure ranking/verification helpers are tested above; these cover the
# orchestration in `proposals.discover_website` itself — the strong-pick
# short-circuit, the weak-pick landing-page verify loop, its error fallback,
# and the empty-results case (review finding on #67 / PR #99).


def test_discover_website_strong_pick_skips_verification(monkeypatch):
    from app.agents.assistant import proposals

    def fake_search(query):
        return {
            "results": [
                {"url": "https://random.example"},  # scores 0 — unrelated
                {"url": "https://nimbus.example"},  # exact label match — 1.0
            ]
        }

    async def fake_fetch(url):
        raise AssertionError("a strong pick must not fetch the landing page")

    monkeypatch.setattr(proposals, "web_search", fake_search)
    monkeypatch.setattr(proposals, "fetch_page", fake_fetch)

    assert asyncio.run(proposals.discover_website("Nimbus")) == "nimbus.example"


def test_discover_website_weak_pick_verified_by_landing_page(monkeypatch):
    # The observed FP shape: two weakly name-like hosts tie, and search order
    # alone would pick the wrong one ("foundation"); the landing-page check
    # steers to the host that actually names the company.
    from app.agents.assistant import proposals

    def fake_search(query):
        return {
            "results": [
                {"url": "https://quartzfoundation.example"},
                {"url": "https://quartzhq.example"},
            ]
        }

    async def fake_fetch(url):
        if "quartzhq" in url:
            return {"text": "Quartz Analytics builds developer tooling."}
        return {"text": "The Quartz Foundation is an independent charity."}

    monkeypatch.setattr(proposals, "web_search", fake_search)
    monkeypatch.setattr(proposals, "fetch_page", fake_fetch)

    assert asyncio.run(proposals.discover_website("Quartz Analytics")) == "quartzhq.example"


def test_discover_website_weak_pick_falls_back_on_fetch_errors(monkeypatch):
    # Verification is best-effort: when every fetch errors, the ranked best
    # (here the first-by-search-order tie) is still returned, never None.
    from app.agents.assistant import proposals

    def fake_search(query):
        return {
            "results": [
                {"url": "https://quartzfoundation.example"},
                {"url": "https://quartzhq.example"},
            ]
        }

    async def fake_fetch(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(proposals, "web_search", fake_search)
    monkeypatch.setattr(proposals, "fetch_page", fake_fetch)

    assert asyncio.run(proposals.discover_website("Quartz Analytics")) == "quartzfoundation.example"


def test_discover_website_no_usable_results_returns_none(monkeypatch):
    from app.agents.assistant import proposals

    def fake_search(query):
        return {"results": []}

    monkeypatch.setattr(proposals, "web_search", fake_search)

    assert asyncio.run(proposals.discover_website("Quartz Analytics")) is None

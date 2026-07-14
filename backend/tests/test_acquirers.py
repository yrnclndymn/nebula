"""Pure ranking tests for potential-acquirer analysis (story #44).

DB-free: exercises :func:`rank_acquirer_candidates` and its weights directly, so
the heuristic is verifiable without Neo4j. The graph-gathering half is covered by
``test_acquirers_graph.py`` (skips without a database; CI is the arbiter).
Fictional company names only (public-repo rule).
"""

from app.graph.acquirers import (
    ACTIVITY_CAP,
    W_ACTIVITY,
    W_DIRECT_PARTNER,
    W_KIND_DEAL,
    W_SHARED_CLIENT,
    W_SHARED_PARTNER,
    W_TOPIC_DEAL,
    rank_acquirer_candidates,
)


def _cand(name, **kw):
    base = {
        "acquirer": name,
        "topic_deals": [],
        "kind_deals": [],
        "shared_partners": [],
        "shared_clients": [],
        "is_direct_partner": False,
        "total_acquisitions": 0,
    }
    base.update(kw)
    return base


def test_topic_deal_scores_and_carries_why_with_source():
    src = "https://news.example/acme-buys-foo"
    ranked = rank_acquirer_candidates(
        [_cand("Acme", topic_deals=[{"target": "Foo", "source": src}], total_acquisitions=1)]
    )
    assert len(ranked) == 1
    row = ranked[0]
    assert row["acquirer"] == "Acme"
    assert row["score"] == W_TOPIC_DEAL
    reasons = {r["signal"]: r["detail"] for r in row["why"]}
    assert reasons["acquired-in-topic"]["count"] == 1
    assert reasons["acquired-in-topic"]["deals"] == [{"target": "Foo", "source": src}]
    # A single acquisition earns no activity bonus and no active-acquirer reason.
    assert "active-acquirer" not in reasons


def test_kind_deal_reason_carries_target_kind():
    ranked = rank_acquirer_candidates(
        [_cand("Acme", kind_deals=[{"target": "Bar", "source": None}])],
        target_kind="isv",
    )
    detail = ranked[0]["why"][0]["detail"]
    assert ranked[0]["why"][0]["signal"] == "acquired-same-kind"
    assert detail["kind"] == "isv" and detail["count"] == 1
    assert ranked[0]["score"] == W_KIND_DEAL


def test_partner_and_client_overlap_scored():
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Globex",
                shared_partners=["P1", "P2"],
                shared_clients=["C1"],
                is_direct_partner=True,
            )
        ]
    )
    row = ranked[0]
    assert row["score"] == W_DIRECT_PARTNER + 2 * W_SHARED_PARTNER + W_SHARED_CLIENT
    signals = {r["signal"] for r in row["why"]}
    assert signals == {"direct-partner", "shared-partners", "shared-clients"}


def test_activity_bonus_is_capped():
    # A serial acquirer with a single topic tie: activity bonus counts deals beyond
    # the first, capped at ACTIVITY_CAP, so volume can't dominate.
    ranked = rank_acquirer_candidates(
        [_cand("Serial", topic_deals=[{"target": "Foo", "source": "s"}], total_acquisitions=99)]
    )
    row = ranked[0]
    assert row["score"] == W_TOPIC_DEAL + W_ACTIVITY * ACTIVITY_CAP
    assert row["total_acquisitions"] == 99
    active = next(r for r in row["why"] if r["signal"] == "active-acquirer")
    assert active["detail"]["total_acquisitions"] == 99


def test_pure_activity_without_relevance_is_dropped():
    # Acquisition history but zero tie to the target -> not a candidate.
    assert rank_acquirer_candidates([_cand("Unrelated", total_acquisitions=10)]) == []


def test_ordering_is_by_score_then_name():
    strong = _cand(
        "Zeta", topic_deals=[{"target": "A", "source": "s"}, {"target": "B", "source": "s"}]
    )
    mid = _cand("Beta", shared_partners=["P1"], is_direct_partner=True)  # 2 + 3 = 5
    tie_a = _cand("Alpha", shared_clients=["C1"])  # 2
    tie_z = _cand("Omega", shared_clients=["C9"])  # 2 — same score, name breaks tie
    ranked = rank_acquirer_candidates([tie_z, mid, strong, tie_a])
    assert [r["acquirer"] for r in ranked] == ["Zeta", "Beta", "Alpha", "Omega"]
    assert ranked[0]["score"] == 2 * W_TOPIC_DEAL


def test_limit_caps_results():
    cands = [_cand(f"Co{i}", shared_clients=[f"C{i}"], total_acquisitions=i + 1) for i in range(10)]
    assert len(rank_acquirer_candidates(cands, limit=3)) == 3


def test_dedup_deals_and_names():
    # Duplicate targets / names from the graph collapse to one each.
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                topic_deals=[
                    {"target": "Foo", "source": "s1"},
                    {"target": "Foo", "source": "s2"},
                ],
                shared_partners=["P1", "P1"],
            )
        ]
    )
    reasons = {r["signal"]: r["detail"] for r in ranked[0]["why"]}
    assert reasons["acquired-in-topic"]["count"] == 1
    assert reasons["acquired-in-topic"]["deals"] == [{"target": "Foo", "source": "s1"}]
    assert reasons["shared-partners"]["partners"] == ["P1"]
    assert ranked[0]["score"] == W_TOPIC_DEAL + W_SHARED_PARTNER


# --- Route auth (PR #121 review; precedent: test_acquisition_endpoints_require_auth)


def test_acquirer_endpoints_require_auth():
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    settings.require_auth = True
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert (
                client.get("/companies/Acme%20__pytest44__/potential-acquirers").status_code == 401
            )
            assert client.get("/ma/active-acquirers").status_code == 401
    finally:
        settings.require_auth = False

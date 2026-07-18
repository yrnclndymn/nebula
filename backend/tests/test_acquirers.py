"""Pure ranking tests for potential-acquirer analysis (story #44).

DB-free: exercises :func:`rank_acquirer_candidates` and its weights directly, so
the heuristic is verifiable without Neo4j. The graph-gathering half is covered by
``test_acquirers_graph.py`` (skips without a database; CI is the arbiter).
Fictional company names only (public-repo rule).
"""

from app.graph.acquirers import (
    ACTIVITY_CAP,
    SIZE_LARGER_RATIO,
    W_ACTIVITY,
    W_DIRECT_PARTNER,
    W_KIND_DEAL,
    W_SHARED_CLIENT,
    W_SHARED_PARTNER,
    W_SIZE_FIT,
    W_SIZE_PLAUSIBLE,
    W_SIZE_SMALLER,
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
        "acquirer_headcount": None,
        "past_target_headcounts": [],
        "past_target_amounts": [],
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


# --- Size awareness (#165): relative company size + historical target-size fit.
# Every size signal fires ONLY when both sides of its comparison exist; missing size
# data is neutral (see the regression guard at the end of this block).


def _reason(row, signal):
    return next((r for r in row["why"] if r["signal"] == signal), None)


def test_size_plausible_bonus_when_acquirer_meaningfully_larger():
    # A relationship tie (so it is a candidate) plus acquirer >= 3x the target.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=6000)],
        target_headcount=200,
    )
    row = ranked[0]
    assert row["score"] == W_SHARED_CLIENT + W_SIZE_PLAUSIBLE
    reason = _reason(row, "size-plausible")
    assert reason["detail"]["direction"] == "larger"
    assert reason["detail"]["acquirer_headcount"] == 6000
    assert reason["detail"]["target_headcount"] == 200
    assert reason["detail"]["ratio"] == 30.0


def test_size_plausible_penalty_when_acquirer_smaller_never_excludes():
    # Reverse-takeover shape: acquirer smaller than the target -> dampened, not dropped.
    ranked = rank_acquirer_candidates(
        [_cand("Tiny", shared_clients=["C1"], acquirer_headcount=30)],
        target_headcount=500,
    )
    assert len(ranked) == 1  # never excluded
    row = ranked[0]
    assert row["score"] == W_SHARED_CLIENT + W_SIZE_SMALLER
    reason = _reason(row, "size-plausible")
    assert reason["detail"]["direction"] == "smaller"


def test_size_plausible_neutral_when_similar_size():
    # Larger, but under the SIZE_LARGER_RATIO threshold -> no size signal at all.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=int(200 * SIZE_LARGER_RATIO) - 1)],
        target_headcount=200,
    )
    row = ranked[0]
    assert row["score"] == W_SHARED_CLIENT
    assert _reason(row, "size-plausible") is None


def test_size_plausible_neutral_when_a_headcount_missing():
    # Acquirer headcount present, target's absent -> both sides not present -> neutral.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=6000)],
        target_headcount=None,
    )
    assert _reason(ranked[0], "size-plausible") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_size_fit_bonus_when_target_within_historical_range():
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                shared_clients=["C1"],
                past_target_headcounts=[50, 80, 120, 200],
            )
        ],
        target_headcount=100,
    )
    row = ranked[0]
    assert row["score"] == W_SHARED_CLIENT + W_SIZE_FIT
    reason = _reason(row, "size-fit")
    assert reason["detail"]["low"] == 50
    assert reason["detail"]["high"] == 200
    assert reason["detail"]["n"] == 4


def test_size_fit_surfaces_cited_amounts_when_present():
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                shared_clients=["C1"],
                past_target_headcounts=[50, 200],
                past_target_amounts=["$100M", "$1.2 billion"],
            )
        ],
        target_headcount=100,
    )
    reason = _reason(ranked[0], "size-fit")
    assert reason["detail"]["amounts"] == ["$100M", "$1.2 billion"]


def test_size_fit_neutral_when_target_outside_range():
    # Target an order of magnitude above the historical range -> no fit signal.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], past_target_headcounts=[10, 20, 30])],
        target_headcount=5000,
    )
    row = ranked[0]
    assert row["score"] == W_SHARED_CLIENT
    assert _reason(row, "size-fit") is None


def test_size_fit_neutral_without_target_or_history():
    # No target headcount -> neutral; no past headcounts -> neutral.
    no_target = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], past_target_headcounts=[50, 80])],
        target_headcount=None,
    )
    assert _reason(no_target[0], "size-fit") is None
    no_history = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], past_target_headcounts=[])],
        target_headcount=100,
    )
    assert _reason(no_history[0], "size-fit") is None


def test_size_signals_never_rescue_a_non_candidate():
    # A perfect size match but zero relationship tie stays out of the ranking:
    # size reweights candidates, it never gates them in.
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "SizeOnly",
                acquirer_headcount=9000,
                past_target_headcounts=[90, 110],
                total_acquisitions=5,
            )
        ],
        target_headcount=100,
    )
    assert ranked == []


def test_no_size_data_ranks_exactly_as_before():
    # REGRESSION GUARD (#165 acceptance): a rich-relationship candidate with no size
    # data must score, reason, and order IDENTICALLY with or without size awareness.
    rich = _cand(
        "Rich",
        topic_deals=[{"target": "Foo", "source": "s"}],
        shared_partners=["P1"],
        is_direct_partner=True,
        total_acquisitions=4,
    )
    baseline = rank_acquirer_candidates([dict(rich)])  # no target_headcount at all
    with_size = rank_acquirer_candidates([dict(rich)], target_headcount=250)
    assert baseline == with_size
    # And no size signal leaked into the why.
    assert _reason(with_size[0], "size-plausible") is None
    assert _reason(with_size[0], "size-fit") is None


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

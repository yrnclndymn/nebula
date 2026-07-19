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
    W_THESIS_MATCH,
    W_TOPIC_DEAL,
    rank_acquirer_candidates,
)


def _cand(name, **kw):
    base = {
        "acquirer": name,
        "acquirer_kind": None,
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


def _rule(acquirer_kind, target_kind, *, qualifier="", statement="stmt", conf=0.7, evidence=0):
    """A get_thesis_rules-shaped row (see app/graph/thesis.get_thesis_rules)."""
    return {
        "rule_key": f"{acquirer_kind}>{target_kind}|{qualifier}",
        "acquirer_kind": acquirer_kind,
        "target_kind": target_kind,
        "qualifier": qualifier,
        "statement": statement,
        "confidence": conf,
        "origin": "user",
        "updated_at": None,
        "evidence_count": evidence,
    }


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


# --- Thesis-match signal (#194, epic #192): candidate KIND -> target KIND matched
# against the active ThesisRules, fetched once per ranking and threaded in as a
# parameter. Weight scales by rule confidence; qualified rules compose with #165's
# size helpers; missing kind data is strictly neutral (regression-guarded below).


def _thesis_pts(conf):
    return round(W_THESIS_MATCH * conf, 2)


def test_thesis_match_scores_and_cites_statement_and_evidence():
    rules = [
        _rule(
            "service_provider",
            "service_provider",
            statement="Services buy services.",
            conf=0.7,
            evidence=6,
        )
    ]
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="service_provider", shared_clients=["C1"])],
        target_kind="service_provider",
        thesis_rules=rules,
    )
    row = ranked[0]
    assert row["score"] == W_SHARED_CLIENT + _thesis_pts(0.7)
    reason = _reason(row, "thesis-match")
    assert reason is not None
    assert reason["detail"]["statement"] == "Services buy services."
    assert reason["detail"]["confidence"] == 0.7
    assert reason["detail"]["evidence"] == 6
    assert reason["detail"]["acquirer_kind"] == "service_provider"
    assert reason["detail"]["target_kind"] == "service_provider"
    # An unqualified rule carries no qualifier key.
    assert "qualifier" not in reason["detail"]


def test_thesis_points_scale_with_confidence():
    low = rank_acquirer_candidates(
        [_cand("Low", acquirer_kind="cloud_provider", shared_clients=["C1"])],
        target_kind="service_provider",
        thesis_rules=[_rule("cloud_provider", "service_provider", conf=0.3)],
    )[0]
    high = rank_acquirer_candidates(
        [_cand("High", acquirer_kind="cloud_provider", shared_clients=["C1"])],
        target_kind="service_provider",
        thesis_rules=[_rule("cloud_provider", "service_provider", conf=0.9)],
    )[0]
    assert high["score"] > low["score"]
    assert high["score"] == W_SHARED_CLIENT + _thesis_pts(0.9)


def test_thesis_no_match_when_kinds_differ():
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="service_provider",
        thesis_rules=[_rule("service_provider", "service_provider")],
    )
    assert _reason(ranked[0], "thesis-match") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_thesis_neutral_when_candidate_kind_missing():
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind=None, shared_clients=["C1"])],
        target_kind="service_provider",
        thesis_rules=[_rule("service_provider", "service_provider")],
    )
    assert _reason(ranked[0], "thesis-match") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_thesis_neutral_when_target_kind_missing():
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="service_provider", shared_clients=["C1"])],
        target_kind=None,
        thesis_rules=[_rule("service_provider", "service_provider")],
    )
    assert _reason(ranked[0], "thesis-match") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_thesis_qualifier_normalised_across_cosmetic_variants():
    # Kinds arrive already normalised from the DB, but a differently-cased target kind
    # (e.g. from an older write) still matches — the matcher normalises both sides.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="Service Provider", shared_clients=["C1"])],
        target_kind="Service_Provider",
        thesis_rules=[_rule("service_provider", "service_provider", conf=0.7)],
    )
    assert _reason(ranked[0], "thesis-match") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + _thesis_pts(0.7)


# --- The domain-focused ISV rule composes with #165's size-plausibility: it fires
# only when the acquirer is meaningfully LARGER than the target.

_ISV_RULE = _rule(
    "service_provider",
    "isv",
    qualifier="domain-focused",
    statement="Larger SPs buy ISVs.",
    conf=0.5,
)


def test_thesis_qualified_rule_fires_only_when_acquirer_larger():
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Big",
                acquirer_kind="service_provider",
                shared_clients=["C1"],
                acquirer_headcount=6000,
            )
        ],
        target_kind="isv",
        target_headcount=200,
        thesis_rules=[_ISV_RULE],
    )
    row = ranked[0]
    reason = _reason(row, "thesis-match")
    assert reason is not None
    assert reason["detail"]["qualifier"] == "domain-focused"
    # Both the thesis match AND the #165 size-plausible bonus fire off the same size fact.
    assert row["score"] == W_SHARED_CLIENT + W_SIZE_PLAUSIBLE + _thesis_pts(0.5)


def test_thesis_qualified_rule_neutral_when_acquirer_not_larger():
    # Similar size (under the larger-ratio threshold) -> qualified rule does not fire.
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Peer",
                acquirer_kind="service_provider",
                shared_clients=["C1"],
                acquirer_headcount=220,
            )
        ],
        target_kind="isv",
        target_headcount=200,
        thesis_rules=[_ISV_RULE],
    )
    row = ranked[0]
    assert _reason(row, "thesis-match") is None
    assert row["score"] == W_SHARED_CLIENT


def test_thesis_qualified_rule_neutral_without_size_data():
    # No headcount either side -> the 'larger' condition is unconfirmable -> neutral.
    ranked = rank_acquirer_candidates(
        [_cand("Unknown", acquirer_kind="service_provider", shared_clients=["C1"])],
        target_kind="isv",
        target_headcount=None,
        thesis_rules=[_ISV_RULE],
    )
    assert _reason(ranked[0], "thesis-match") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_thesis_unknown_qualifier_stays_neutral():
    # A qualifier the matcher can't operationalize does not fire unearned points.
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                acquirer_kind="service_provider",
                shared_clients=["C1"],
                acquirer_headcount=6000,
            )
        ],
        target_kind="service_provider",
        target_headcount=200,
        thesis_rules=[_rule("service_provider", "service_provider", qualifier="regulated")],
    )
    assert _reason(ranked[0], "thesis-match") is None


def test_thesis_never_rescues_a_non_candidate():
    # A perfect kind match but zero relationship tie stays out of the ranking: the
    # thesis reweights candidates, it never gates them in (the #165 discipline).
    ranked = rank_acquirer_candidates(
        [_cand("KindOnly", acquirer_kind="service_provider", total_acquisitions=5)],
        target_kind="service_provider",
        thesis_rules=[_rule("service_provider", "service_provider")],
    )
    assert ranked == []


def test_no_kind_data_ranks_identically_with_thesis_rules():
    # REGRESSION GUARD (#194, mirroring #165): a rich-relationship candidate with no
    # acquirer_kind must score, reason, and order BYTE-IDENTICALLY whether or not
    # thesis rules are supplied.
    rich = _cand(
        "Rich",
        acquirer_kind=None,
        topic_deals=[{"target": "Foo", "source": "s"}],
        shared_partners=["P1"],
        is_direct_partner=True,
        total_acquisitions=4,
    )
    rules = [
        _rule("service_provider", "service_provider", conf=0.7, evidence=6),
        _rule("cloud_provider", "service_provider", conf=0.75),
    ]
    baseline = rank_acquirer_candidates([dict(rich)], target_kind="service_provider")
    with_rules = rank_acquirer_candidates(
        [dict(rich)], target_kind="service_provider", thesis_rules=rules
    )
    assert baseline == with_rules
    assert _reason(with_rules[0], "thesis-match") is None


# --- Mutation hardening (#206): exact-kill the scoring-weight / threshold / detail-field
# survivors that wave-014 left behind. Existing tests assert PRESENCE and ORDERING; these
# pin the arithmetic (point values as behavioural contract), boundary-test the ratio /
# tolerance / confidence edges, and assert every `why` detail field the SPA renders. Kept
# DB-free so they kill regardless of whether a Neo4j is up.


def test_dedup_deals_keeps_distinct_target_after_a_duplicate():
    # `continue`-not-`break`: a repeat target must be SKIPPED, not end the scan — a
    # distinct deal after it still counts. (kills the loop-`break` mutant.)
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                topic_deals=[
                    {"target": "Foo", "source": "s1"},
                    {"target": "Foo", "source": "s2"},
                    {"target": "Bar", "source": "s3"},
                ],
            )
        ]
    )
    detail = _reason(ranked[0], "acquired-in-topic")["detail"]
    assert detail["count"] == 2
    assert [d["target"] for d in detail["deals"]] == ["Foo", "Bar"]
    assert ranked[0]["score"] == 2 * W_TOPIC_DEAL


def test_zero_headcount_is_neutral_not_a_size_signal():
    # `_pos_int`: a 0 headcount is missing data, not a real size — no size-plausible
    # signal (kills the `> 0`→`>= 0` mutant that would read 0 as "smaller").
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=0)],
        target_headcount=200,
    )
    assert _reason(ranked[0], "size-plausible") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_headcount_of_one_is_a_valid_positive_size():
    # `_pos_int`: a headcount of exactly 1 is a real (if tiny) size, not missing —
    # a 3x-larger acquirer over a target of 1 still fires (kills the `> 0`→`> 1` mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=3)],
        target_headcount=1,
    )
    reason = _reason(ranked[0], "size-plausible")
    assert reason is not None and reason["detail"]["direction"] == "larger"
    assert ranked[0]["score"] == W_SHARED_CLIENT + W_SIZE_PLAUSIBLE


def test_size_plausible_fires_exactly_at_the_larger_ratio_boundary():
    # ratio == SIZE_LARGER_RATIO must count as "larger" (>=, not >): 600 / 200 == 3.0.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=600)],
        target_headcount=200,
    )
    reason = _reason(ranked[0], "size-plausible")
    assert reason is not None and reason["detail"]["direction"] == "larger"
    assert reason["detail"]["ratio"] == SIZE_LARGER_RATIO
    assert ranked[0]["score"] == W_SHARED_CLIENT + W_SIZE_PLAUSIBLE


def test_equal_size_is_neutral_not_a_smaller_penalty():
    # ratio == 1.0 (equal headcount) is neutral, NOT a reverse-takeover penalty
    # (kills the `< 1`→`<= 1` mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=200)],
        target_headcount=200,
    )
    assert _reason(ranked[0], "size-plausible") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_size_plausible_ratio_rounds_to_one_decimal():
    # The rendered ratio is rounded to ONE decimal: 1000 / 300 == 3.333… → 3.3, not
    # 3 (round(x)/round(x, None)) and not 3.33 (round(x, 2)).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], acquirer_headcount=1000)],
        target_headcount=300,
    )
    assert _reason(ranked[0], "size-plausible")["detail"]["ratio"] == 3.3


def test_size_fit_lower_tolerance_admits_target_below_observed_low():
    # A target just BELOW the historical low still fits (low / TOLERANCE, not low *
    # TOLERANCE): past [100, 300], target 80 sits inside [66.7, 450].
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], past_target_headcounts=[100, 300])],
        target_headcount=80,
    )
    assert _reason(ranked[0], "size-fit") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + W_SIZE_FIT


def test_size_fit_includes_target_exactly_at_lower_bound():
    # target == low / TOLERANCE is INSIDE the band (<=, not <): past [150, 300],
    # 150 / 1.5 == 100.0, target 100 fits.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], past_target_headcounts=[150, 300])],
        target_headcount=100,
    )
    assert _reason(ranked[0], "size-fit") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + W_SIZE_FIT


def test_size_fit_includes_target_exactly_at_upper_bound():
    # target == high * TOLERANCE is INSIDE the band (<=, not <): past [100],
    # 100 * 1.5 == 150.0, target 150 fits.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], past_target_headcounts=[100])],
        target_headcount=150,
    )
    assert _reason(ranked[0], "size-fit") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + W_SIZE_FIT


def test_size_fit_upper_tolerance_admits_target_above_observed_high():
    # A target above the historical high still fits (high * TOLERANCE, not high /
    # TOLERANCE): past [100, 300], target 250 sits inside [66.7, 450] but ABOVE
    # high / 1.5 == 200 (kills the `*`→`/` upper-bound mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"], past_target_headcounts=[100, 300])],
        target_headcount=250,
    )
    assert _reason(ranked[0], "size-fit") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + W_SIZE_FIT


def test_size_fit_amounts_filter_drops_blank_strings():
    # The cited-amounts filter is `isinstance(str) AND non-blank`: a whitespace-only
    # amount is dropped (kills the `and`→`or` mutant that would keep it).
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                shared_clients=["C1"],
                past_target_headcounts=[50, 200],
                past_target_amounts=["   ", "$5M"],
            )
        ],
        target_headcount=100,
    )
    assert _reason(ranked[0], "size-fit")["detail"]["amounts"] == ["$5M"]


def test_both_size_signals_sum_rather_than_overwrite():
    # When size-plausible AND size-fit both fire they ADD (kills the `+=`→`=` mutant
    # in `_size_signals` that would keep only the last).
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                shared_clients=["C1"],
                acquirer_headcount=6000,
                past_target_headcounts=[100, 300],
            )
        ],
        target_headcount=200,
    )
    row = ranked[0]
    assert {_reason(row, "size-plausible") is not None, _reason(row, "size-fit") is not None} == {
        True
    }
    assert row["score"] == W_SHARED_CLIENT + W_SIZE_PLAUSIBLE + W_SIZE_FIT


def test_thesis_garbage_confidence_falls_back_to_prior_half():
    # A non-numeric confidence falls back to the 0.5 seed prior (kills the fallback
    # 0.5→1.5 mutant, which the [0, 1] clamp would otherwise hide as 1.0).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("isv", "isv", conf=None)],
    )
    reason = _reason(ranked[0], "thesis-match")
    assert reason["detail"]["confidence"] == 0.5
    assert ranked[0]["score"] == W_SHARED_CLIENT + _thesis_pts(0.5)


def test_thesis_confidence_clamps_to_one():
    # A confidence above 1 clamps to 1.0 (kills the upper-clamp 1.0→2.0 mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("isv", "isv", conf=5.0)],
    )
    reason = _reason(ranked[0], "thesis-match")
    assert reason["detail"]["confidence"] == 1.0
    assert ranked[0]["score"] == W_SHARED_CLIENT + W_THESIS_MATCH


def test_thesis_points_round_to_two_decimals():
    # Thesis points round to TWO decimals: 3 * 0.111 == 0.333 → 0.33, not 0.333.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("isv", "isv", conf=0.111)],
    )
    assert ranked[0]["score"] == W_SHARED_CLIENT + 0.33


def test_thesis_evidence_zero_renders_as_zero_not_one():
    # A rule with no evidence renders evidence 0, not a phantom 1 (kills the
    # `or 0`→`or 1` mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("isv", "isv", conf=0.5, evidence=0)],
    )
    assert _reason(ranked[0], "thesis-match")["detail"]["evidence"] == 0


def test_thesis_neutral_when_only_target_kind_missing_even_with_a_none_kind_rule():
    # Target kind absent must short-circuit to neutral even if a rule's target kind is
    # itself blank (would normalise to None and spuriously match) — pins the `or` in the
    # missing-kind guard (kills the `or`→`and` guard mutants).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind=None,
        thesis_rules=[_rule("isv", "", conf=0.9)],
    )
    assert _reason(ranked[0], "thesis-match") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_thesis_neutral_when_only_candidate_kind_missing_even_with_a_none_kind_rule():
    # Symmetric guard for the acquirer side: candidate kind absent is neutral even if a
    # rule's acquirer kind is blank.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind=None, shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("", "isv", conf=0.9)],
    )
    assert _reason(ranked[0], "thesis-match") is None
    assert ranked[0]["score"] == W_SHARED_CLIENT


def test_thesis_scans_all_rules_past_an_acquirer_kind_mismatch():
    # A non-matching acquirer-kind rule must be SKIPPED, not end the scan — a later
    # matching rule still fires (kills the first `continue`→`break` mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("cloud_provider", "isv", conf=0.7), _rule("isv", "isv", conf=0.7)],
    )
    assert _reason(ranked[0], "thesis-match") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + _thesis_pts(0.7)


def test_thesis_scans_all_rules_past_a_target_kind_mismatch():
    # Likewise past a target-kind mismatch (kills the second `continue`→`break` mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("isv", "cloud_provider", conf=0.7), _rule("isv", "isv", conf=0.7)],
    )
    assert _reason(ranked[0], "thesis-match") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + _thesis_pts(0.7)


def test_thesis_scans_all_rules_past_a_failed_qualifier():
    # A rule whose qualifier does not hold is SKIPPED, not fatal — a later unqualified
    # match still fires (kills the qualifier `continue`→`break` mutant).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[
            _rule("isv", "isv", qualifier="regulated", conf=0.7),
            _rule("isv", "isv", conf=0.7),
        ],
    )
    assert _reason(ranked[0], "thesis-match") is not None
    assert ranked[0]["score"] == W_SHARED_CLIENT + _thesis_pts(0.7)


def test_thesis_multiple_matching_rules_accumulate():
    # Two matching rules ADD their confidence-scaled points and each carries its own
    # reason (kills the `+=`→`=` mutant that would keep only the last).
    ranked = rank_acquirer_candidates(
        [_cand("Acme", acquirer_kind="isv", shared_clients=["C1"])],
        target_kind="isv",
        thesis_rules=[_rule("isv", "isv", conf=0.6), _rule("isv", "isv", conf=0.4)],
    )
    row = ranked[0]
    assert len([r for r in row["why"] if r["signal"] == "thesis-match"]) == 2
    assert row["score"] == W_SHARED_CLIENT + _thesis_pts(0.6) + _thesis_pts(0.4)


def test_relevance_gate_sums_topic_and_kind_signals():
    # The relevance gate ADDS topic and kind ties (kills the `+`→`-` mutant that would
    # let one topic deal cancel one kind deal to exactly 0 and drop the candidate).
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                topic_deals=[{"target": "Foo", "source": "s"}],
                kind_deals=[{"target": "Bar", "source": "s"}],
            )
        ],
        target_kind="isv",
    )
    assert len(ranked) == 1
    assert ranked[0]["score"] == W_TOPIC_DEAL + W_KIND_DEAL


def test_relevance_gate_sums_topic_and_client_signals():
    # Likewise a topic tie and a shared-client tie both count toward relevance (kills
    # the shared-clients `+`→`-` mutant).
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                topic_deals=[{"target": "Foo", "source": "s"}],
                shared_clients=["C1"],
            )
        ]
    )
    assert len(ranked) == 1
    assert ranked[0]["score"] == W_TOPIC_DEAL + W_SHARED_CLIENT


def test_absent_acquisition_count_reports_zero():
    # A relevant candidate with no acquisition history reports total_acquisitions 0,
    # not a phantom 1 (kills the `or 0`→`or 1` mutant on the total).
    ranked = rank_acquirer_candidates([_cand("Acme", shared_clients=["C1"])])
    assert ranked[0]["total_acquisitions"] == 0


def test_activity_bonus_counts_the_second_acquisition():
    # Two acquisitions earn exactly one activity point (bonus counts deals BEYOND the
    # first: total - 1), and the active-acquirer reason appears at total > 1. Kills
    # both the `total - 1`→`total - 2` and `total > 1`→`total > 2` mutants.
    ranked = rank_acquirer_candidates([_cand("Acme", shared_clients=["C1"], total_acquisitions=2)])
    row = ranked[0]
    assert row["score"] == W_SHARED_CLIENT + W_ACTIVITY
    active = _reason(row, "active-acquirer")
    assert active is not None and active["detail"]["total_acquisitions"] == 2


def test_why_details_expose_every_field_the_ui_renders():
    # Pin the exact detail keys/values the SPA reads for the kind / partner / client
    # signals (kills the detail-dict key/value rename mutants).
    ranked = rank_acquirer_candidates(
        [
            _cand(
                "Acme",
                kind_deals=[{"target": "Bar", "source": "s"}],
                shared_partners=["P1", "P2"],
                shared_clients=["C1"],
                is_direct_partner=True,
            )
        ],
        target_kind="isv",
    )
    reasons = {r["signal"]: r for r in ranked[0]["why"]}

    kind_detail = reasons["acquired-same-kind"]["detail"]
    assert kind_detail["deals"] == [{"target": "Bar", "source": "s"}]

    # direct-partner carries an explicit (empty) `detail` key.
    assert reasons["direct-partner"]["detail"] == {}

    partner_detail = reasons["shared-partners"]["detail"]
    assert partner_detail["count"] == 2
    assert partner_detail["partners"] == ["P1", "P2"]

    client_reason = reasons["shared-clients"]
    assert client_reason["detail"]["count"] == 1
    assert client_reason["detail"]["clients"] == ["C1"]


def test_tiebreak_orders_case_insensitively():
    # Equal scores break by name CASE-INSENSITIVELY (`.lower()`): "_Legacy" (a leading
    # gap char) sorts before "Acme" under lower-casing but after it under upper-casing,
    # so this distinguishes `.lower()` from the `.upper()` mutant.
    ranked = rank_acquirer_candidates(
        [_cand("Acme", shared_clients=["C1"]), _cand("_Legacy", shared_clients=["C2"])]
    )
    assert [r["acquirer"] for r in ranked] == ["_Legacy", "Acme"]


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

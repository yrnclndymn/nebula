"""Signal retention — prune-selection logic (#37).

Pure/deterministic: no DB, fictional names only (public repo). Covers the two
caps (newest-N per company per kind, and max age), the "kept iff it clears BOTH
for some company" rule, shared-signal survival, orphan pruning, and the
un-reviewed-work protection hook. The scheduled runner + graph integration are
exercised in test_schedules.py.
"""

from datetime import datetime, timedelta, timezone

from app.graph.retention import SignalRef, select_signals_to_prune

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _sig(url: str, *, kind: str = "news", age_days: float = 0, companies=("Acme",)) -> SignalRef:
    return SignalRef(
        url=url,
        kind=kind,
        effective_at=NOW - timedelta(days=age_days),
        companies=tuple(companies),
    )


def _prune(signals, *, max_per_company=3, max_age_days=365, protected=frozenset()):
    return select_signals_to_prune(
        signals,
        max_per_company=max_per_company,
        max_age_days=max_age_days,
        now=NOW,
        protected_urls=protected,
    )


def test_empty_input_prunes_nothing():
    assert _prune([]) == []


def test_count_cap_keeps_newest_n_prunes_the_rest():
    # Five recent news for Acme, cap 3 → the two oldest are pruned.
    sigs = [_sig(f"u{i}", age_days=i) for i in range(5)]
    pruned = _prune(sigs, max_per_company=3)
    assert pruned == ["u3", "u4"]


def test_under_cap_and_recent_keeps_everything():
    sigs = [_sig(f"u{i}", age_days=i) for i in range(3)]
    assert _prune(sigs, max_per_company=3, max_age_days=365) == []


def test_age_cap_prunes_old_signal_even_when_under_count_cap():
    # Only two signals (under a cap of 3), but one is older than max age.
    sigs = [_sig("fresh", age_days=10), _sig("stale", age_days=400)]
    assert _prune(sigs, max_per_company=3, max_age_days=365) == ["stale"]


def test_kept_requires_clearing_both_caps():
    # "keep" needs within-count AND within-age. Two distinct failure modes:
    #   * globex_old is rank-1 for its company (within count) but too old → pruned
    #   * acme_overflow is recent (within age) but rank-3 with cap 2 → pruned
    sigs = [
        _sig("acme_new", age_days=1, companies=("Acme",)),  # rank 1, fresh → kept
        _sig("acme_mid", age_days=2, companies=("Acme",)),  # rank 2, fresh → kept
        _sig("acme_overflow", age_days=3, companies=("Acme",)),  # rank 3 → over count
        _sig("globex_old", age_days=400, companies=("Globex",)),  # rank 1 but too old
    ]
    pruned = _prune(sigs, max_per_company=2, max_age_days=365)
    assert pruned == ["acme_overflow", "globex_old"]


def test_kinds_are_counted_independently():
    # 3 news + 3 blog for Acme, cap 2 per kind → oldest of each kind pruned.
    sigs = [_sig(f"n{i}", kind="news", age_days=i) for i in range(3)]
    sigs += [_sig(f"b{i}", kind="blog", age_days=i) for i in range(3)]
    assert _prune(sigs, max_per_company=2) == ["b2", "n2"]


def test_shared_signal_survives_if_kept_by_any_company():
    # `shared` is rank-3 (overflow) for Globex but rank-1 for Acme → kept, because
    # deleting the node would remove it from Acme too.
    sigs = [
        _sig("g0", age_days=1, companies=("Globex",)),
        _sig("g1", age_days=2, companies=("Globex",)),
        _sig("shared", age_days=3, companies=("Globex", "Acme")),
    ]
    assert _prune(sigs, max_per_company=2) == []


def test_shared_signal_pruned_only_when_overflow_for_all_companies():
    # `shared` overflows for BOTH companies → pruned.
    sigs = [
        _sig("a0", age_days=1, companies=("Acme",)),
        _sig("a1", age_days=2, companies=("Acme",)),
        _sig("g0", age_days=1, companies=("Globex",)),
        _sig("g1", age_days=2, companies=("Globex",)),
        _sig("shared", age_days=9, companies=("Acme", "Globex")),
    ]
    assert _prune(sigs, max_per_company=2) == ["shared"]


def test_orphan_signal_with_no_company_is_pruned():
    # No company means it clears no count cap → pruned regardless of freshness.
    assert _prune([_sig("orphan", age_days=1, companies=())]) == ["orphan"]


def test_protected_urls_are_never_pruned():
    # `stale` would be pruned by age, but it is cited by un-reviewed work.
    sigs = [_sig("fresh", age_days=1), _sig("stale", age_days=400)]
    assert _prune(sigs, protected=frozenset({"stale"})) == []


def test_ranking_is_deterministic_at_the_boundary():
    # Two signals share an effective date exactly on the cap boundary; the URL
    # tie-break makes the selection stable across runs/input order.
    sigs = [
        _sig("aaa", age_days=5),
        _sig("bbb", age_days=5),
        _sig("ccc", age_days=1),
    ]
    first = _prune(sigs, max_per_company=2)
    second = _prune(list(reversed(sigs)), max_per_company=2)
    assert first == second == ["aaa"]  # "bbb" > "aaa", so aaa is the rank-3 loser

"""Signal-refresh selection — pure due-company logic (#36).

Pure/deterministic: no DB, fictional names only (public repo). Covers the
staleness rule (never-captured + stale-beyond-threshold are due; fresh is not),
stalest-first ranking, never-captured sorting first, the batch cap, and
deterministic tie-breaks. The scheduled runner + graph fan-out are exercised in
test_schedules.py.
"""

from datetime import datetime, timedelta, timezone

from app.graph.refresh import RefreshCandidate, select_companies_to_refresh

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _c(name: str, *, captured_days_ago: float | None = 0) -> RefreshCandidate:
    last = None if captured_days_ago is None else NOW - timedelta(days=captured_days_ago)
    return RefreshCandidate(
        name=name, website=f"https://{name.lower()}.example", last_captured_at=last
    )


def _select(candidates, *, staleness_days=7, batch_size=25):
    return select_companies_to_refresh(
        candidates, staleness_days=staleness_days, now=NOW, batch_size=batch_size
    )


def _names(candidates):
    return [c.name for c in candidates]


def test_empty_input_selects_nothing():
    assert _select([]) == []


def test_fresh_company_is_not_due():
    # Captured yesterday, staleness 7 → not due.
    assert _select([_c("Acme", captured_days_ago=1)]) == []


def test_stale_company_is_due():
    assert _names(_select([_c("Acme", captured_days_ago=10)])) == ["Acme"]


def test_never_captured_company_is_due():
    # No signals yet (None) counts as maximally stale → always due.
    assert _names(_select([_c("Globex", captured_days_ago=None)])) == ["Globex"]


def test_boundary_exactly_at_threshold_is_not_yet_due():
    # Captured exactly staleness_days ago is NOT strictly older than the cutoff.
    assert _select([_c("Acme", captured_days_ago=7)], staleness_days=7) == []
    # A hair past the threshold is due.
    assert _names(_select([_c("Acme", captured_days_ago=7.001)], staleness_days=7)) == ["Acme"]


def test_stalest_first_ranking():
    cands = [
        _c("Fresh", captured_days_ago=1),  # not due
        _c("Mid", captured_days_ago=20),
        _c("Old", captured_days_ago=100),
        _c("Never", captured_days_ago=None),
    ]
    # Never-captured is stalest, then oldest capture; Fresh is excluded.
    assert _names(_select(cands)) == ["Never", "Old", "Mid"]


def test_batch_cap_takes_the_neediest():
    cands = [_c(f"C{i:02d}", captured_days_ago=100 - i) for i in range(10)]
    # All 10 are stale; cap 3 keeps the three stalest (largest days-ago = C00..C02).
    assert _names(_select(cands, batch_size=3)) == ["C00", "C01", "C02"]


def test_zero_batch_selects_nothing():
    # The budget rail can close the tap entirely.
    assert _select([_c("Acme", captured_days_ago=100)], batch_size=0) == []


def test_tie_break_by_name_is_deterministic():
    # Same capture age → name breaks the tie, stable across input order.
    a = _c("Alpha", captured_days_ago=30)
    b = _c("Bravo", captured_days_ago=30)
    assert _names(_select([a, b], batch_size=1)) == ["Alpha"]
    assert _names(_select([b, a], batch_size=1)) == ["Alpha"]


def test_never_captured_tie_break_by_name():
    # Two never-captured companies tie on staleness → name orders them.
    x = _c("Xdev", captured_days_ago=None)
    y = _c("Ydev", captured_days_ago=None)
    assert _names(_select([y, x], batch_size=1)) == ["Xdev"]

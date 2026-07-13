"""Pure assembly of a committable :class:`AcquisitionRecord` from raw research.

This is the provenance gate for the M&A write path. The research step (a Gemini
call over untrusted crawled/searched evidence) is asked to cite every deal, but a
claimed citation is not trusted:

- a deal survives ONLY when it names both an ``acquirer`` and a ``target`` and
  carries a valid ``http(s)`` ``source`` URL (the deal's existence must be
  checkable), and
- a deal's ``amount``/``currency`` survive ONLY when a *separate* valid
  ``amount_source`` URL backs the figure — the "drop-uncited-numbers" guardrail
  for financials (acceptance #43 / the standing no-figure-without-a-citation rule).
  An uncited amount is dropped; the rest of the deal still commits.

Pure (no DB, no network, no model): easy to reason about and test-first.
"""

from app.agents.ma.models import AcquisitionRecord, AcquisitionResearch, Deal, DealResearch


def valid_source(url: str | None) -> bool:
    """A citation URL we will render and store: a real ``http(s)`` URL only.

    Guards the review surface (sources render as clickable links) against a hostile
    ``javascript:``/``data:`` scheme sneaking through the untrusted model output —
    the same guard the people/discovery extractors apply.
    """
    return bool(url) and url.strip().lower().startswith(("http://", "https://"))


def _clean(value: str | None) -> str | None:
    """Trim to a non-empty string, else None."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_deal(raw: DealResearch) -> Deal | None:
    """Derive a committable :class:`Deal` from raw research, or None if it fails
    the provenance gate. See the module docstring for the rules."""
    acquirer = _clean(raw.acquirer)
    target = _clean(raw.target)
    if not acquirer or not target:
        return None
    if not valid_source(raw.source):
        return None  # deal existence must be cited

    # The money guardrail: keep amount/currency only when the amount is itself
    # cited by a valid URL. An uncited figure is dropped (the deal still commits).
    amount = _clean(raw.amount)
    currency = _clean(raw.currency)
    if amount and not valid_source(raw.amount_source):
        amount = None
        currency = None
    amount_source = raw.amount_source.strip() if (amount and raw.amount_source) else None
    if not amount:
        currency = None  # currency is meaningless without a value

    return Deal(
        acquirer=acquirer,
        target=target,
        announced_at=_clean(raw.announced_at),
        closed_at=_clean(raw.closed_at),
        amount=amount,
        currency=currency,
        thesis=_clean(raw.thesis),
        source=raw.source.strip(),
        amount_source=amount_source,
    )


def _dedup_deals(deals: list[Deal]) -> list[Deal]:
    """De-duplicate by (acquirer, target), keeping the first (richest-first order
    is the caller's concern) — the ACQUIRED edge is keyed on the same pair."""
    out: list[Deal] = []
    seen: set[tuple[str, str]] = set()
    for d in deals:
        key = (d.acquirer, d.target)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def build_acquisition_record(research: AcquisitionResearch, company: str) -> AcquisitionRecord:
    """Derive the committable, provenance-filtered record from raw research.

    Every deal passes through :func:`build_deal` (uncited deals dropped, uncited
    amounts stripped) and duplicates on (acquirer, target) are collapsed.
    """
    built = [d for d in (build_deal(r) for r in research.deals) if d is not None]
    return AcquisitionRecord(company=company, deals=_dedup_deals(built))


def diff_acquisitions(existing: list[dict] | None, record: AcquisitionRecord) -> list[dict]:
    """A compact per-deal diff for the review surface (pure).

    ``existing`` is the list of ACQUIRED edges already stored for the subject (see
    :func:`app.graph.acquisitions.get_acquisitions`), each as
    ``{acquirer, target, amount, ...}``. Returns one ``{deal, status, ...}`` entry
    per proposed deal: ``"new"`` when the (acquirer, target) pair isn't stored yet,
    ``"update"`` when it is but the amount would change, and nothing for deals that
    already match — so the reviewer sees only what would actually change.
    """
    existing = existing or []
    stored = {(e.get("acquirer"), e.get("target")): e for e in existing}
    changes: list[dict] = []
    for d in record.deals:
        prev = stored.get((d.acquirer, d.target))
        if prev is None:
            changes.append({"deal": d.model_dump(), "status": "new"})
        elif d.amount and d.amount != prev.get("amount"):
            changes.append(
                {"deal": d.model_dump(), "status": "update", "old_amount": prev.get("amount")}
            )
    return changes

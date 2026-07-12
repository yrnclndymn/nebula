"""Date normalisation for captured signal items.

The graph write path (``signals.upsert_signal``) already re-parses whatever string
lands in ``SignalRecord.published_at`` via ``signals.parse_published_at`` (ISO 8601
+ a few common human formats). This module is the *pre-normaliser*: it reuses that
parser and adds the cases it can't handle — RSS ``pubDate`` (RFC 2822) and relative
phrases ("yesterday", "2 days ago") — emitting an **ISO 8601 string** so the write
path then stores a real temporal date instead of falling back to the raw string.

Pure and deterministic (``now`` is injectable so relative dates are testable).
"""

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from app.graph.signals import parse_published_at

# "2 days ago", "1 week ago", "an hour ago" — unit -> timedelta factory.
_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}
_RELATIVE_RE = re.compile(r"^(?:(\d+)|an?)\s+(second|minute|hour|day|week)s?\s+ago$", re.IGNORECASE)


def _parse_relative(text: str, now: datetime) -> datetime | None:
    low = text.lower()
    if low == "today":
        return now
    if low == "yesterday":
        return now - timedelta(days=1)
    match = _RELATIVE_RE.match(low)
    if not match:
        return None
    count = int(match.group(1)) if match.group(1) else 1
    return now - timedelta(seconds=count * _UNIT_SECONDS[match.group(2)])


def normalise_date(raw: str | None, now: datetime | None = None) -> str | None:
    """Return an ISO 8601 string for ``raw``, or ``None`` if it can't be recognised.

    Tries, in order: the existing ISO/human parser, RFC 2822 (feed ``pubDate``),
    then relative phrases. The result is timezone-aware (naive inputs are pinned to
    UTC). ``now`` defaults to the current UTC time and only affects relative dates.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    now = now or datetime.now(timezone.utc)

    dt = parse_published_at(text)
    if dt is None:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        dt = _parse_relative(text, now)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

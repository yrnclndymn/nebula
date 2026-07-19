"""Retry + rate-limit wrapper for Gemini calls.

Two failure modes to survive on the free tier:

- **Transient errors** (429 rate-limit + 5xx) on a *direct* `generate_content`
  call: `generate_with_retry` retries with backoff, honouring a server-supplied
  RetryInfo delay when the 429 carries one (else exponential from 2s).
- **429 RESOURCE_EXHAUSTED on the ADK enrichment path**, where the model call is
  buried inside a `Runner` turn we can't wrap per-request: `run_with_quota_retry`
  wraps a whole `enrich()` run — on a quota 429 it waits the indicated delay and
  re-runs the run (retry is at *run* granularity; the page-cache makes the
  re-crawl cheap). After bounded attempts it raises `QuotaExhausted`, whose
  `.message` is a human one-liner for the Job node and `.detail` the raw error.

Every Gemini caller also acquires from the shared process-wide rate limiter
(`app.ratelimit`) so chat + jobs pace themselves against one free-tier ceiling.
"""

import asyncio
import re

from google import genai
from google.genai import errors

from app import budget, ratelimit

_RETRYABLE = {429, 500, 502, 503, 504}

# Fallback wait when a 429 RESOURCE_EXHAUSTED carries no explicit RetryInfo, and
# the ceiling we never sleep past on a single quota backoff (a minute clears the
# per-minute window).
_DEFAULT_QUOTA_DELAY = 30.0
_MAX_QUOTA_DELAY = 60.0

# "retryDelay": "42s" / retry_delay: 42.5s — Google returns the RetryInfo hint in
# the error payload; we parse it out of the stringified error (works for both the
# structured google-genai APIError and a plain exception off the ADK path).
_RETRY_DELAY_RE = re.compile(r"retry_?[Dd]elay['\"\s:=]+(\d+(?:\.\d+)?)\s*s")


def quota_retry_delay(exc: BaseException) -> float | None:
    """If `exc` is a rate-limit / quota-exhaustion error, the delay to wait
    (seconds) before retrying — the server's RetryInfo hint if present, else
    `0.0`. Returns `None` when `exc` is *not* a quota error, so callers can tell
    "wait and retry" apart from "not my problem, re-raise".

    Recognises Gemini's shape (`.code`/`.status`, RESOURCE_EXHAUSTED text) and,
    since the #8 provider seam, litellm's openai-shaped errors too
    (`.status_code == 429` / a RateLimitError class) — `run_with_quota_retry`
    wraps whole research runs regardless of the configured provider."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    status_code = getattr(exc, "status_code", None)
    text = str(exc)
    is_quota = (
        code == 429
        or status_code == 429
        or status == "RESOURCE_EXHAUSTED"
        or "RESOURCE_EXHAUSTED" in text
        or type(exc).__name__ == "RateLimitError"
    )
    if not is_quota:
        return None
    match = _RETRY_DELAY_RE.search(text)
    return float(match.group(1)) if match else 0.0


def _bounded_quota_delay(delay: float) -> float:
    """A quota backoff wait: the hint if it's positive, else a default, capped."""
    return min(delay if delay > 0 else _DEFAULT_QUOTA_DELAY, _MAX_QUOTA_DELAY)


class QuotaExhausted(Exception):
    """Raised when a run keeps hitting 429 RESOURCE_EXHAUSTED after its retries.

    `.message` is a concise, human-readable one-liner (for the Job node); the raw
    error text is preserved on `.detail` rather than dumped into the message.
    """

    def __init__(self, cause: BaseException):
        from app.config import settings

        rpm = settings.gemini_rpm
        self.message = f"Gemini quota exhausted — free tier allows {rpm}/min; retry shortly"
        self.detail = str(cause)
        super().__init__(self.message)


async def generate_with_retry(client: genai.Client, *, max_attempts: int = 8, **kwargs):
    """Call `client.aio.models.generate_content(**kwargs)`, retrying transient errors.

    Charges one LLM call against the active per-run budget (see `app.budget`)
    before the request — so a budgeted run stops before an over-cap call, while
    interactive callers (no budget installed) are unaffected. Charged once per
    logical call, not per retry. Each actual attempt also acquires a slot from the
    shared rate limiter (`app.ratelimit`) so it paces against the free-tier rpm."""
    budget.charge_llm()
    delay = 2.0
    for attempt in range(max_attempts):
        try:
            await ratelimit.acquire()
            return await client.aio.models.generate_content(**kwargs)
        except errors.APIError as exc:
            if exc.code in _RETRYABLE and attempt < max_attempts - 1:
                # Prefer the server's RetryInfo hint on a 429; else exp backoff.
                hint = quota_retry_delay(exc)
                await asyncio.sleep(hint if hint else delay)
                delay = min(delay * 2, 60.0)
                continue
            raise
    raise RuntimeError("unreachable")


async def run_with_quota_retry(factory, *, max_attempts: int | None = None, sleep=asyncio.sleep):
    """Run `factory()` (an async call), retrying it whole on 429 RESOURCE_EXHAUSTED.

    For the ADK enrichment path, where the model call is inside a `Runner` turn we
    can't wrap per-request: on a quota 429 we wait the server-indicated delay and
    re-run `factory` from the top (retry is at run granularity). Non-quota errors
    propagate unchanged. After `max_attempts` (default `settings.quota_retry_attempts`)
    it raises `QuotaExhausted`, carrying a friendly message + the raw detail."""
    if max_attempts is None:
        from app.config import settings

        max_attempts = settings.quota_retry_attempts
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
            delay = quota_retry_delay(exc)
            if delay is None:
                raise  # not a quota error — don't swallow it
            last_exc = exc
            if attempt >= max_attempts - 1:
                raise QuotaExhausted(exc) from exc
            await sleep(_bounded_quota_delay(delay))
    raise QuotaExhausted(last_exc)  # pragma: no cover — loop always returns/raises

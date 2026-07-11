"""Retry wrapper for transient Gemini errors (429 rate-limit + 5xx).

Free-tier limits are strict (e.g. flash-lite is 15 req/min), so bursty workloads
like the eval harness need backoff. Starts at 2s and doubles, which comfortably
clears a per-minute window.
"""

import asyncio

from google import genai
from google.genai import errors

from app import budget

_RETRYABLE = {429, 500, 502, 503, 504}


async def generate_with_retry(client: genai.Client, *, max_attempts: int = 8, **kwargs):
    """Call `client.aio.models.generate_content(**kwargs)`, retrying transient errors.

    Charges one LLM call against the active per-run budget (see `app.budget`)
    before the request — so a budgeted run stops before an over-cap call, while
    interactive callers (no budget installed) are unaffected. Charged once per
    logical call, not per retry."""
    budget.charge_llm()
    delay = 2.0
    for attempt in range(max_attempts):
        try:
            return await client.aio.models.generate_content(**kwargs)
        except errors.APIError as exc:
            if exc.code in _RETRYABLE and attempt < max_attempts - 1:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            raise
    raise RuntimeError("unreachable")

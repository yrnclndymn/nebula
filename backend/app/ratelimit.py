"""Process-wide async rate limiter for Gemini calls.

Free-tier Gemini quota is a **requests-per-minute** ceiling on the shared API key
(e.g. flash-lite is ~15 req/min). Two things spend against that one budget inside
a single Cloud Run instance: interactive chat and background research jobs. Left
unthrottled they starve each other — a batch of jobs firing all at once burns the
minute's quota and the next caller 429s.

This module is that shared throttle: **one limiter per process**, acquired by
every Gemini caller (see `app.genai_retry.generate_with_retry` and the ADK
enrichment run). It is a *rate* limiter — it paces requests over time — and is
complementary to `app.budget`, which caps total *spend* per run.

Design: a sliding-window reservation limiter. Each `acquire()` records the time
its slot is *granted*; the Nth grant is scheduled no earlier than `window`
seconds after the (N-rpm)th grant. That yields a smooth `rpm`/window rate with no
thundering herd, and — crucially — **zero added latency when under budget** (the
grant is `now`, the wait is 0), so interactive chat stays responsive.

`rpm <= 0` disables limiting entirely (paid tier / no ceiling): `acquire()` is a
no-op. The limiter is safe to share across coroutines on one event loop (an
`asyncio.Lock` serialises the tiny slot computation); Cloud Run runs one process
per instance, which is the scope we care about.
"""

import asyncio
from collections import deque


class RateLimiter:
    """A sliding-window requests-per-`window` limiter.

    `clock`/`sleep` are injectable so tests can drive it on a fake clock without
    real waiting; production uses the running loop's monotonic clock and
    `asyncio.sleep`.
    """

    def __init__(self, rpm: int, *, window: float = 60.0, clock=None, sleep=None):
        self.rpm = rpm
        self.window = window
        self._clock = clock or self._loop_time
        self._sleep = sleep or asyncio.sleep
        self._grants: deque[float] = deque()  # scheduled times of the last <=rpm grants
        self._lock = asyncio.Lock()

    @staticmethod
    def _loop_time() -> float:
        return asyncio.get_running_loop().time()

    async def acquire(self) -> None:
        """Block until a request slot is available, then consume it.

        Returns immediately (no sleep) whenever the current rate is under `rpm`.
        A non-positive `rpm` means "no limit" and returns at once.
        """
        if self.rpm <= 0:
            return
        async with self._lock:
            now = self._clock()
            if len(self._grants) >= self.rpm:
                # The oldest grant that still counts toward the window is the one
                # `rpm` slots back; this slot may open no sooner than `window`
                # after it. `max(now, …)` keeps us honest when we're idle.
                earliest = self._grants[-self.rpm]
                grant = max(now, earliest + self.window)
            else:
                grant = now
            self._grants.append(grant)
            while len(self._grants) > self.rpm:
                self._grants.popleft()
            wait = grant - now
        if wait > 0:
            await self._sleep(wait)


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    """The process-wide limiter, built lazily from settings on first use.

    Built lazily (not at import) so `settings.gemini_rpm` — which may be loaded
    from the environment after import — is read at the point of use.
    """
    global _limiter
    if _limiter is None:
        from app.config import settings

        _limiter = RateLimiter(settings.gemini_rpm)
    return _limiter


def reset_limiter() -> None:
    """Drop the cached limiter so the next `get_limiter()` rebuilds it from
    current settings. For tests that vary `gemini_rpm`."""
    global _limiter
    _limiter = None


async def acquire() -> None:
    """Acquire one Gemini request slot from the shared limiter."""
    await get_limiter().acquire()

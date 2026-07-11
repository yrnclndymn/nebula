"""Gemini quota resilience (issue #65): shared rate limiter + 429-aware retry.

Pure unit tests — no Neo4j, no network, no real waiting. The limiter and the
retry wrapper both take injectable clock/sleep seams so time is deterministic.
"""

import asyncio

import pytest
from google.genai import errors

from app import ratelimit
from app.config import settings
from app.genai_retry import (
    QuotaExhausted,
    generate_with_retry,
    quota_retry_delay,
    run_with_quota_retry,
)
from app.ratelimit import RateLimiter


class FakeClock:
    """A hand-cranked clock: time only advances when `sleep` is awaited."""

    def __init__(self):
        self.t = 0.0

    def time(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.t += d


# --- rate limiter --------------------------------------------------------------


def test_limiter_passes_through_under_budget():
    # The first `rpm` acquires in an empty window never sleep — interactive chat
    # stays responsive.
    clock = FakeClock()
    lim = RateLimiter(rpm=3, window=60.0, clock=clock.time, sleep=clock.sleep)

    async def run():
        for _ in range(3):
            await lim.acquire()
        return clock.t

    assert asyncio.run(run()) == 0.0  # never slept


def test_limiter_enforces_rpm():
    clock = FakeClock()
    lim = RateLimiter(rpm=3, window=60.0, clock=clock.time, sleep=clock.sleep)

    async def run():
        waits = []
        for _ in range(6):
            before = clock.t
            await lim.acquire()
            waits.append(clock.t - before)
        return waits

    waits = asyncio.run(run())
    assert waits[:3] == [0.0, 0.0, 0.0]  # a full window's worth passes immediately
    assert waits[3] == 60.0  # the 4th must wait for the window to roll


def test_limiter_disabled_when_rpm_zero():
    # rpm=0 (paid tier) disables limiting: unlimited acquires, never sleeps.
    clock = FakeClock()
    lim = RateLimiter(rpm=0, window=60.0, clock=clock.time, sleep=clock.sleep)

    async def run():
        for _ in range(100):
            await lim.acquire()
        return clock.t

    assert asyncio.run(run()) == 0.0


def test_get_limiter_reads_settings_and_resets(monkeypatch):
    monkeypatch.setattr(settings, "gemini_rpm", 7)
    ratelimit.reset_limiter()
    try:
        assert ratelimit.get_limiter().rpm == 7
    finally:
        ratelimit.reset_limiter()  # don't leak the fake limiter into other tests


# --- 429 parsing ---------------------------------------------------------------


class _FakeAPIError(Exception):
    """Stands in for a genai error off the ADK path: carries code/status like the
    real one and a message that embeds the RetryInfo hint."""

    def __init__(self, msg, code=None, status=None):
        super().__init__(msg)
        self.code = code
        self.status = status


def test_quota_retry_delay_parses_retry_info():
    exc = _FakeAPIError(
        '429 RESOURCE_EXHAUSTED. retryDelay: "17s"', code=429, status="RESOURCE_EXHAUSTED"
    )
    assert quota_retry_delay(exc) == 17.0


def test_quota_retry_delay_zero_when_no_hint():
    # Recognised as a quota error (so we retry) but with no explicit delay → 0.0.
    assert quota_retry_delay(Exception("RESOURCE_EXHAUSTED, no hint here")) == 0.0


def test_quota_retry_delay_none_for_non_quota():
    assert quota_retry_delay(Exception("500 internal boom")) is None
    assert quota_retry_delay(_FakeAPIError("boom", code=500)) is None


# --- run_with_quota_retry (whole-run retry for the ADK enrich path) -------------


def test_run_with_quota_retry_recovers_on_a_later_attempt():
    calls = {"n": 0}
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeAPIError('RESOURCE_EXHAUSTED retryDelay: "2s"', code=429)
        return "done"

    out = asyncio.run(run_with_quota_retry(factory, max_attempts=3, sleep=fake_sleep))
    assert out == "done"
    assert calls["n"] == 2  # re-ran the whole factory once
    assert sleeps == [2.0]  # honoured the server's RetryInfo delay


def test_run_with_quota_retry_bounds_and_raises_friendly():
    calls = {"n": 0}
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    async def factory():
        calls["n"] += 1
        raise _FakeAPIError('RESOURCE_EXHAUSTED retryDelay: "5s"', code=429)

    async def run():
        with pytest.raises(QuotaExhausted) as ei:
            await run_with_quota_retry(factory, max_attempts=3, sleep=fake_sleep)
        return ei.value

    exc = asyncio.run(run())
    assert calls["n"] == 3  # bounded at max_attempts, then gives up
    assert sleeps == [5.0, 5.0]  # slept between the 3 attempts, not after the last
    assert "quota exhausted" in exc.message.lower()
    assert "/min" in exc.message  # human one-liner, not a raw JSON dump
    assert "RESOURCE_EXHAUSTED" in exc.detail  # raw error kept separately


def test_run_with_quota_retry_reraises_non_quota_errors():
    calls = {"n": 0}

    async def fake_sleep(d):  # pragma: no cover — must never be called
        raise AssertionError("should not sleep on a non-quota error")

    async def factory():
        calls["n"] += 1
        raise ValueError("some other failure")

    async def run():
        with pytest.raises(ValueError):
            await run_with_quota_retry(factory, max_attempts=3, sleep=fake_sleep)

    asyncio.run(run())
    assert calls["n"] == 1  # not retried


def test_quota_exhausted_keeps_raw_out_of_the_one_liner():
    exc = QuotaExhausted(Exception("raw 429 blob {huge json}"))
    assert "retry shortly" in exc.message
    assert "raw 429 blob" in exc.detail  # raw preserved for debugging
    assert "raw 429 blob" not in exc.message  # but NOT dumped into the one-liner


# --- generate_with_retry: limiter wiring + RetryInfo-aware backoff -------------


def test_generate_with_retry_honours_retry_info(monkeypatch):
    # Isolate from the shared limiter singleton — this test is about the backoff.
    async def _noop():
        return None

    monkeypatch.setattr(ratelimit, "acquire", _noop)
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr("app.genai_retry.asyncio.sleep", fake_sleep)

    attempts = {"n": 0}

    class _Models:
        async def generate_content(self, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise errors.APIError(
                    429,
                    {"error": {"status": "RESOURCE_EXHAUSTED", "message": 'retryDelay: "3s"'}},
                )
            return "ok"

    class _Client:
        aio = type("Aio", (), {"models": _Models()})()

    out = asyncio.run(generate_with_retry(_Client(), max_attempts=4))
    assert out == "ok"
    assert attempts["n"] == 2
    assert sleeps == [3.0]  # used the server hint, not the default 2s backoff

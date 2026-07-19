"""Provider-selection seam (#8): pure-logic tests for `app.llm`.

No live LLM calls — the litellm / genai call surfaces are monkeypatched. These pin
the switch behaviour: LLM_PROVIDER unset (or "gemini") keeps the native google-genai
path byte-for-byte; a non-gemini provider routes ADK model selection through LiteLlm
and direct structured calls through litellm.acompletion.
"""

import asyncio

import pydantic
import pytest

from app import llm
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_provider(monkeypatch):
    """Default every test to the shipped defaults; each test opts into overrides."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "llm_model", "")


class _Schema(pydantic.BaseModel):
    answer: str
    count: int = 0


# --- provider selection -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["gemini", "GEMINI", "  Gemini ", "google", "google-genai", ""],
)
def test_is_gemini_true_for_default_and_google_aliases(monkeypatch, value):
    monkeypatch.setattr(settings, "llm_provider", value)
    assert llm.is_gemini() is True
    assert llm.use_litellm() is False


@pytest.mark.parametrize("value", ["anthropic", "openai", "Anthropic", "azure"])
def test_use_litellm_true_for_non_gemini(monkeypatch, value):
    monkeypatch.setattr(settings, "llm_provider", value)
    assert llm.is_gemini() is False
    assert llm.use_litellm() is True


# --- model resolution ---------------------------------------------------------


def test_resolve_model_prefers_llm_model_when_set(monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "anthropic/claude-x")
    assert llm.resolve_model("gemini-3.1-flash-lite") == "anthropic/claude-x"


def test_resolve_model_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "")
    assert llm.resolve_model("gemini-3.1-flash-lite") == "gemini-3.1-flash-lite"


# --- ADK model selection ------------------------------------------------------


def test_adk_model_is_plain_string_for_gemini():
    model = llm.adk_model("gemini-3.1-flash-lite")
    assert model == "gemini-3.1-flash-lite"
    assert isinstance(model, str)


def test_adk_model_is_litellm_for_non_gemini(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "anthropic/claude-x")
    from google.adk.models.lite_llm import LiteLlm

    model = llm.adk_model("gemini-3.1-flash-lite")
    assert isinstance(model, LiteLlm)
    assert model.model == "anthropic/claude-x"


def test_adk_model_defaults_to_agent_model(monkeypatch):
    monkeypatch.setattr(settings, "agent_model", "gemini-agent-default")
    assert llm.adk_model() == "gemini-agent-default"


# --- generate() routing -------------------------------------------------------


def test_generate_gemini_delegates_to_generate_with_retry(monkeypatch):
    """Gemini path must go through the existing genai_retry wrapper untouched."""
    calls = {}

    async def fake_retry(client, *, model, contents, config):
        calls["model"] = model
        calls["contents"] = contents
        return "GEMINI_RESP"

    monkeypatch.setattr(llm, "generate_with_retry", fake_retry)
    # A stand-in genai client so we don't construct a real one.
    sentinel_client = object()
    resp = asyncio.run(
        llm.generate(
            model="gemini-3.1-flash-lite",
            contents="hi",
            config=None,
            client=sentinel_client,
        )
    )
    assert resp == "GEMINI_RESP"
    assert calls["model"] == "gemini-3.1-flash-lite"


def test_generate_gemini_applies_llm_model_override(monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "gemini-override")
    seen = {}

    async def fake_retry(client, *, model, contents, config):
        seen["model"] = model
        return "R"

    monkeypatch.setattr(llm, "generate_with_retry", fake_retry)
    asyncio.run(
        llm.generate(model="gemini-3.1-flash-lite", contents="x", config=None, client=object())
    )
    assert seen["model"] == "gemini-override"


def test_generate_litellm_structured_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "anthropic/claude-x")

    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _FakeCompletion('{"answer": "yes", "count": 3}')

    charged = {"n": 0}
    acquired = {"n": 0}
    monkeypatch.setattr(
        llm.budget, "charge_llm", lambda: charged.__setitem__("n", charged["n"] + 1)
    )

    async def fake_acquire():
        acquired["n"] += 1

    monkeypatch.setattr(llm.ratelimit, "acquire", fake_acquire)
    monkeypatch.setattr(llm, "_litellm_acompletion", fake_acompletion)

    def boom(*a, **k):  # ensure the gemini retry path is NOT used
        raise AssertionError("gemini path must not run for a non-gemini provider")

    monkeypatch.setattr(llm, "generate_with_retry", boom)

    config = _FakeConfig(response_schema=_Schema, temperature=0.0)
    resp = asyncio.run(llm.generate(model="anthropic/claude-x", contents="prompt", config=config))

    assert resp.text == '{"answer": "yes", "count": 3}'
    parsed = resp.parsed
    assert isinstance(parsed, _Schema)
    assert parsed.answer == "yes" and parsed.count == 3
    # provider paced against the shared limiter + budget, exactly once each
    assert charged["n"] == 1
    assert acquired["n"] == 1
    # schema + temperature translated onto the litellm call
    assert captured["model"] == "anthropic/claude-x"
    assert captured["response_format"] is _Schema
    assert captured["temperature"] == 0.0
    assert captured["messages"] == [{"role": "user", "content": "prompt"}]


def test_generate_litellm_bad_json_parsed_is_none(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "anthropic/claude-x")

    async def fake_acompletion(**kwargs):
        return _FakeCompletion("this is not json")

    monkeypatch.setattr(llm.budget, "charge_llm", lambda: None)

    async def fake_acquire():
        return None

    monkeypatch.setattr(llm.ratelimit, "acquire", fake_acquire)
    monkeypatch.setattr(llm, "_litellm_acompletion", fake_acompletion)

    config = _FakeConfig(response_schema=_Schema, temperature=0.0)
    resp = asyncio.run(llm.generate(model="anthropic/claude-x", contents="p", config=config))
    # Mirrors google-genai's `.parsed`: None when the payload can't be validated.
    assert resp.parsed is None
    assert resp.text == "this is not json"


def test_generate_litellm_no_schema_text_only(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "anthropic/claude-x")

    async def fake_acompletion(**kwargs):
        assert "response_format" not in kwargs  # no schema → no structured request
        return _FakeCompletion("just prose")

    monkeypatch.setattr(llm.budget, "charge_llm", lambda: None)

    async def fake_acquire():
        return None

    monkeypatch.setattr(llm.ratelimit, "acquire", fake_acquire)
    monkeypatch.setattr(llm, "_litellm_acompletion", fake_acompletion)

    config = _FakeConfig(response_schema=None, temperature=0.2)
    resp = asyncio.run(llm.generate(model="anthropic/claude-x", contents="p", config=config))
    assert resp.text == "just prose"
    assert resp.parsed is None


def test_generate_litellm_rejects_multimodal(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "anthropic/claude-x")
    monkeypatch.setattr(llm.budget, "charge_llm", lambda: None)

    # A non-string `contents` (e.g. Gemini image parts) is unsupported on the
    # litellm text path — must fail loudly, not silently drop the images.
    with pytest.raises(NotImplementedError):
        asyncio.run(llm.generate(model="anthropic/claude-x", contents=[{"parts": []}], config=None))


# --- test doubles -------------------------------------------------------------


class _FakeConfig:
    def __init__(self, response_schema=None, temperature=None):
        self.response_schema = response_schema
        self.temperature = temperature


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


# --- misconfiguration guard (#8 review finding) --------------------------------


def test_resolve_model_rejects_non_gemini_without_llm_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "")
    with pytest.raises(ValueError, match="requires LLM_MODEL"):
        llm.resolve_model("gemini-3.1-flash-lite")


def test_adk_model_rejects_non_gemini_without_llm_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "")
    with pytest.raises(ValueError, match="requires LLM_MODEL"):
        llm.adk_model()


# --- litellm retry loop (#8 review finding) ------------------------------------


class _FakeRateLimitError(Exception):
    def __init__(self, status_code=429):
        super().__init__("rate limited")
        self.status_code = status_code


def _litellm_env(monkeypatch, acquired):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "anthropic/claude-x")
    monkeypatch.setattr(llm.budget, "charge_llm", lambda: None)

    async def fake_acquire():
        acquired["n"] += 1

    monkeypatch.setattr(llm.ratelimit, "acquire", fake_acquire)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)


def test_litellm_retries_transient_and_reacquires_limiter(monkeypatch):
    acquired = {"n": 0}
    _litellm_env(monkeypatch, acquired)
    calls = {"n": 0}

    async def flaky_acompletion(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeRateLimitError()
        return _FakeCompletion("ok")

    monkeypatch.setattr(llm, "_litellm_acompletion", flaky_acompletion)
    resp = asyncio.run(llm.generate(model="anthropic/claude-x", contents="p", config=None))
    assert resp.text == "ok"
    assert calls["n"] == 3
    # every ATTEMPT paces against the shared limiter, not just the first
    assert acquired["n"] == 3


def test_litellm_non_retryable_raises_immediately(monkeypatch):
    acquired = {"n": 0}
    _litellm_env(monkeypatch, acquired)

    async def bad_request(**kwargs):
        raise _FakeRateLimitError(status_code=400)

    monkeypatch.setattr(llm, "_litellm_acompletion", bad_request)
    with pytest.raises(_FakeRateLimitError):
        asyncio.run(llm.generate(model="anthropic/claude-x", contents="p", config=None))
    assert acquired["n"] == 1


def test_litellm_retries_exhaust_then_raise(monkeypatch):
    acquired = {"n": 0}
    _litellm_env(monkeypatch, acquired)

    async def always_429(**kwargs):
        raise _FakeRateLimitError()

    monkeypatch.setattr(llm, "_litellm_acompletion", always_429)
    with pytest.raises(_FakeRateLimitError):
        asyncio.run(llm.generate(model="anthropic/claude-x", contents="p", config=None))
    assert acquired["n"] == llm._MAX_ATTEMPTS

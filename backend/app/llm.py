"""Provider-switchable LLM seam (#8).

Agents + extraction historically called Gemini directly (google-genai / ADK). This
module is the single choke point that lets the model be swapped by config with no
code edits, per DEPLOYMENT.md Phase E.

Two surfaces, one decision:

- **ADK agents** call `adk_model(default)` for their `model=` argument. For the
  default provider ("gemini") this returns a plain model *string* — exactly what the
  agents passed before — so ADK keeps its native Gemini path. For any other provider
  it returns a `google.adk.models.lite_llm.LiteLlm(model=...)`.
- **Direct structured callers** (importer/extract, field_extract, digest, the capture
  and research paths, logos) call `generate(...)`. For gemini it delegates verbatim to
  `app.genai_retry.generate_with_retry` — same client, same retry/backoff, same
  budget + rate-limit pacing as before. For a non-gemini provider it routes through
  `litellm.acompletion`, still acquiring the shared process-wide rate limiter and
  charging the per-run budget (one ceiling across providers).

**Behaviour-preserving default:** with `LLM_PROVIDER` unset (or "gemini") and
`LLM_MODEL` unset, every path here is byte-for-byte the pre-#8 behaviour. LiteLLM is
only imported and engaged when a non-gemini provider is configured.

`genai_retry` stays Gemini-specific (its 429/RetryInfo parsing is Gemini's error
shape); LiteLLM errors are NOT forced through it — litellm does its own bounded
retries via `num_retries`.
"""

import json
import logging
from typing import Any

from app import budget, ratelimit
from app.config import settings
from app.genai_retry import generate_with_retry

logger = logging.getLogger(__name__)

# Provider strings that mean "use the native google-genai path". Anything else is
# treated as a LiteLLM provider family (anthropic, openai, azure, ...).
_GEMINI_PROVIDERS = {"", "gemini", "google", "google-genai", "google_genai"}


def provider() -> str:
    """The configured provider, normalised (lowercased, trimmed). "" → gemini."""
    return (settings.llm_provider or "").strip().lower()


def is_gemini() -> bool:
    """True when the native google-genai path should be used (the default)."""
    return provider() in _GEMINI_PROVIDERS


def use_litellm() -> bool:
    """True when calls should route through LiteLLM (a non-gemini provider)."""
    return not is_gemini()


def resolve_model(default: str) -> str:
    """The effective model id: the `LLM_MODEL` pass-through if set, else `default`.

    `LLM_MODEL` is used verbatim (e.g. "anthropic/claude-...", "gpt-..."), so a
    provider switch never needs a code edit. Unset keeps the caller's existing
    default (`gemini_model` for direct calls, `agent_model` for agents)."""
    return settings.llm_model or default


def adk_model(default: str | None = None):
    """Model argument for an ADK `Agent`: a plain string for gemini, else `LiteLlm`.

    `default` is the agent's historical model (defaults to `settings.agent_model`).
    LiteLlm is imported lazily so the gemini path never touches the extension."""
    model = resolve_model(default or settings.agent_model)
    if is_gemini():
        return model
    from google.adk.models.lite_llm import LiteLlm  # lazy: extensions dep

    return LiteLlm(model=model)


class _LiteLLMResponse:
    """Adapter over a litellm completion exposing the `.text` / `.parsed` surface the
    google-genai callers already read.

    `.parsed` mirrors google-genai semantics: the validated pydantic instance when a
    `response_schema` was requested and the payload validates, else `None` (callers
    already guard with `isinstance(parsed, Schema)`)."""

    def __init__(self, text: str, schema: type | None):
        self.text = text
        self._schema = schema

    @property
    def parsed(self):
        if self._schema is None:
            return None
        try:
            return self._schema.model_validate_json(self.text)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "LiteLLM structured output for %s did not validate: %s",
                getattr(self._schema, "__name__", self._schema),
                exc,
            )
            return None


async def _litellm_acompletion(**kwargs: Any):
    """Thin indirection over `litellm.acompletion` (lazy import; patch point in tests)."""
    import litellm

    return await litellm.acompletion(**kwargs)


async def _litellm_generate(*, model: str, contents: Any, config: Any) -> _LiteLLMResponse:
    """Route one structured call through LiteLLM.

    Text prompts only: the Gemini image-parts path (logo vision) can't be expressed
    on this simple text surface, so a non-string `contents` fails loudly rather than
    silently dropping the images. Still acquires the shared rate limiter + charges the
    per-run budget so a non-gemini provider paces against the same ceilings."""
    if not isinstance(contents, str):
        raise NotImplementedError(
            "The LiteLLM path supports text prompts only; multimodal contents "
            "(e.g. Gemini image parts) require the native gemini provider."
        )
    budget.charge_llm()
    await ratelimit.acquire()

    schema = getattr(config, "response_schema", None)
    temperature = getattr(config, "temperature", None)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": contents}],
        # LiteLLM does its own bounded retries — genai_retry is Gemini-specific.
        "num_retries": 2,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if schema is not None:
        # litellm accepts a pydantic model as response_format for JSON-schema output.
        kwargs["response_format"] = schema

    resp = await _litellm_acompletion(**kwargs)
    try:
        text = resp.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        text = ""
    if isinstance(text, (dict, list)):
        text = json.dumps(text)
    return _LiteLLMResponse(text=text, schema=schema)


async def generate(*, model: str, contents: Any, config: Any, client: Any = None):
    """Provider-dispatched structured generation.

    Gemini (default): delegate to `generate_with_retry` — identical client, retry,
    budget, and rate-limit behaviour to the pre-#8 direct callers. `client` is the
    reused `genai.Client` when a caller passes one; otherwise one is constructed here.

    Non-gemini: route through LiteLLM (`_litellm_generate`).

    Returns an object exposing `.text` and `.parsed` in both cases."""
    effective_model = resolve_model(model)
    if is_gemini():
        if client is None:
            from google import genai  # lazy: keep module import light

            client = genai.Client()
        return await generate_with_retry(
            client, model=effective_model, contents=contents, config=config
        )
    return await _litellm_generate(model=effective_model, contents=contents, config=config)

"""Model client. Any OpenAI-compatible chat API works; configured by environment variables:

    LLM_API_KEY           required (or GEMINI_API_KEY)
    LLM_BASE_URL          default: Google Gemini's OpenAI-compatible endpoint
    LLM_MODEL             default: gemini-3.5-flash-lite (free tier, reads images; ~3-5 s per bill,
                          vs ~40 s for gemini-3.8-flash with the same accuracy on our sample bills)
    LLM_VISION_MODEL      optional separate model for reading bill images
    LLM_FALLBACK_MODEL    tried once when the main model is overloaded or rate limited
    LLM_REASONING_EFFORT  sent as reasoning_effort when set (Gemini default: low)

Examples: Groq  LLM_BASE_URL=https://api.groq.com/openai/v1
          Ollama LLM_BASE_URL=http://localhost:11434/v1  LLM_API_KEY=ollama

Every response is validated against a pydantic model; invalid JSON gets one retry with the error.
"""
import json
import os
from pathlib import Path

import openai
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from mediator.tools import inline_schema

# Settings can live in BuildFest/.env (see .env.example); real environment variables win.
load_dotenv(Path(__file__).parent.parent / ".env")

BASE_URL = os.environ.get("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
_GEMINI = "generativelanguage.googleapis.com" in BASE_URL
MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
VISION_MODEL = os.environ.get("LLM_VISION_MODEL", MODEL)
FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "gemini-3.1-flash-lite" if _GEMINI else "")
REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "low" if _GEMINI else "")


class LLMUnavailable(RuntimeError):
    """No key configured, provider down, or rate limited. Callers fall back (manual entry, template script)."""


class LLMBadOutput(RuntimeError):
    """The model returned something that doesn't fit the schema, twice."""


def api_key():
    return os.environ.get("LLM_API_KEY") or os.environ.get("GEMINI_API_KEY")


def configured():
    return bool(api_key())


_client = None


def client():
    global _client
    if not configured():
        raise LLMUnavailable("No model API key configured (set LLM_API_KEY).")
    if _client is None:
        _client = openai.OpenAI(api_key=api_key(), base_url=BASE_URL, timeout=90, max_retries=2)
    return _client


def _call(model, messages, schema_model, use_json_schema):
    kwargs = {"model": model, "messages": messages, "temperature": 0}
    if REASONING_EFFORT:
        kwargs["reasoning_effort"] = REASONING_EFFORT
    if use_json_schema:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema_model.__name__, "schema": inline_schema(schema_model)}}
    else:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client().chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


class _Busy(Exception):
    """Overloaded or rate limited: worth one try on the fallback model."""


def structured(messages, schema_model: type[BaseModel], model=None):
    """Chat call that must return JSON matching schema_model. Returns a validated instance."""
    model = model or MODEL
    try:
        return _structured(messages, schema_model, model)
    except _Busy as e:
        if FALLBACK_MODEL and FALLBACK_MODEL != model:
            try:
                return _structured(messages, schema_model, FALLBACK_MODEL)
            except _Busy as e2:
                raise LLMUnavailable(f"Model busy ({e2})") from e2
        raise LLMUnavailable(f"Model busy ({e})") from e


def _structured(messages, schema_model, model):
    use_json_schema = True
    last_error = None
    for attempt in range(2):
        try:
            text = _call(model, messages, schema_model, use_json_schema)
        except openai.BadRequestError as e:
            if not use_json_schema:
                raise LLMUnavailable(f"Model rejected the request: {e}") from e
            # Provider doesn't support json_schema: ask for plain JSON and put the schema in the prompt.
            use_json_schema = False
            messages = messages + [{"role": "user", "content":
                                    "Reply with JSON only, matching this JSON schema:\n"
                                    + json.dumps(inline_schema(schema_model))}]
            text = _call(model, messages, schema_model, use_json_schema)
        except (openai.RateLimitError, openai.InternalServerError) as e:
            raise _Busy(f"{type(e).__name__} {getattr(e, 'status_code', '')}") from e
        except (openai.APIConnectionError, openai.APITimeoutError,
                openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise LLMUnavailable(f"{type(e).__name__}: {e}") from e
        except openai.APIStatusError as e:
            raise LLMUnavailable(f"Model provider error {e.status_code}") from e
        try:
            return schema_model.model_validate_json(_strip_fences(text))
        except ValidationError as e:
            last_error = e
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "That JSON does not match the required schema. Errors:\n"
                                            + str(e)[:2000] + "\nReturn corrected JSON only."}]
    raise LLMBadOutput(str(last_error)[:500])


def _strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()

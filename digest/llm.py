"""Thin helpers around the Anthropic SDK shared by the research, curation and feedback steps.

Reliability rules applied to every call:
- Each role has an ordered list of models (config.yaml). A model that is unavailable, overloaded,
  refuses, or returns truncated/invalid output is retried once, then the next model is tried.
- Every request streams with a large max_tokens so long outputs are neither cut off nor hit
  HTTP timeouts.
- Authentication/permission errors are not retried: they are configuration problems.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

# Server-side refusal fallback: if a request is declined by a safety classifier, the API
# re-runs it on Anthropic's recommended fallback model instead of returning an empty refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5-1", "claude-fable-5"}
MAX_CONTINUATIONS = 6
MAX_TOKENS = 64000
ATTEMPTS_PER_MODEL = 2

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

_client: anthropic.Anthropic | None = None


class ConfigurationError(RuntimeError):
    """A setup problem (missing/invalid key) that retrying cannot fix."""


class ModelOutputError(RuntimeError):
    """The model answered, but not usably (refusal, truncation, invalid structured output)."""


# Transient or model-specific failures worth retrying on the same or another model.
RETRYABLE = (
    ModelOutputError,
    anthropic.NotFoundError,          # model id unavailable to this account
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.InternalServerError,
    anthropic.ServiceUnavailableError,
    anthropic.APIConnectionError,     # includes timeouts
)


def check_credentials() -> None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ConfigurationError(
            "ANTHROPIC_API_KEY is not set. Add it as a repository secret "
            "(Settings > Secrets and variables > Actions) or export it locally."
        )


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        check_credentials()
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def as_model_list(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def with_model_fallback(models: str | list[str], call: Callable[[str], R], what: str) -> R:
    """Run call(model) over the model list until one succeeds."""
    last: Exception | None = None
    for model in as_model_list(models):
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            try:
                return call(model)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                raise ConfigurationError(f"Anthropic API rejected the credentials: {e}") from e
            except RETRYABLE as e:
                last = e
                log.warning("%s failed on %s (attempt %d): %s", what, model, attempt, e)
                if isinstance(e, anthropic.NotFoundError):
                    break  # this model will not appear on retry; move on
            except anthropic.BadRequestError as e:
                # Usually a parameter this model does not accept; another model may.
                last = e
                log.warning("%s rejected by %s: %s", what, model, e)
                break
    raise RuntimeError(f"{what} failed on all models {as_model_list(models)}: {last}") from last


def _fallback_args(model: str) -> dict:
    """Only send the fallback parameter to models documented to support it."""
    if model in FALLBACK_MODELS:
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}


def _text_of(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text")


def run_with_tools(*, models: str | list[str], system: str, prompt: str, tools: list[dict],
                   effort: str = "high", what: str = "research") -> str:
    """Stream a request that uses server tools (web search/fetch), resuming on pause_turn.

    Returns the concatenated text of the final assistant turn(s).
    """

    def call(model: str) -> str:
        messages: list[dict] = [{"role": "user", "content": prompt}]
        collected: list[str] = []
        for _ in range(MAX_CONTINUATIONS):
            with client().beta.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                **_fallback_args(model),
            ) as stream:
                response = stream.get_final_message()

            if response.stop_reason == "refusal":
                raise ModelOutputError(f"refused ({response.stop_details})")
            collected.append(_text_of(response.content))
            if response.stop_reason != "pause_turn":
                break
            # The server paused its tool loop; send the partial turn back and it resumes.
            messages = [messages[0], {"role": "assistant", "content": response.content}]
        text = "\n".join(t for t in collected if t).strip()
        if not text:
            raise ModelOutputError("empty response")
        return text

    return with_model_fallback(models, call, what)


def parse(*, models: str | list[str], system: str, prompt: str, schema: type[T],
          effort: str = "high", what: str = "structured request") -> T:
    """Streamed request with a structured (Pydantic-validated) response."""

    def call(model: str) -> T:
        try:
            with client().beta.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                **_fallback_args(model),
            ) as stream:
                response = stream.get_final_message()
        except (ValueError, ValidationError) as e:  # SDK parses each text block as it completes
            raise ModelOutputError(f"invalid structured output: {e}") from e

        if response.stop_reason == "refusal":
            raise ModelOutputError(f"refused ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise ModelOutputError("output truncated at max_tokens")
        text = _text_of(response.content)
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            raise ModelOutputError(f"invalid structured output: {e}") from e

    return with_model_fallback(models, call, what)

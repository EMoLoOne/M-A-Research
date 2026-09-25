"""Thin helpers around the Anthropic SDK shared by the research, curation and feedback steps."""

from __future__ import annotations

import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)

# Server-side refusal fallback: if a request is declined by a safety classifier, the API
# re-runs it on Anthropic's recommended fallback model instead of returning an empty refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5-1", "claude-fable-5"}
MAX_CONTINUATIONS = 6

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def _fallback_args(model: str) -> dict:
    """Only send the fallback parameter to models documented to support it."""
    if model in FALLBACK_MODELS:
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}


def _text_of(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text")


def run_with_tools(*, model: str, system: str, prompt: str, tools: list[dict], effort: str = "high") -> str:
    """Stream a request that uses server tools (web search/fetch), resuming on pause_turn.

    Returns the concatenated text of the final assistant turn(s).
    """
    messages: list[dict] = [{"role": "user", "content": prompt}]
    collected: list[str] = []
    for _ in range(MAX_CONTINUATIONS):
        with client().beta.messages.stream(
            model=model,
            max_tokens=64000,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            **_fallback_args(model),
        ) as stream:
            response = stream.get_final_message()

        if response.stop_reason == "refusal":
            log.warning("Research request refused (%s); skipping", response.stop_details)
            break
        collected.append(_text_of(response.content))
        if response.stop_reason != "pause_turn":
            break
        # The server paused its tool loop; send the partial turn back and it resumes.
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    return "\n".join(t for t in collected if t)


def parse(*, model: str, system: str, prompt: str, schema: type[T], effort: str = "high") -> T:
    """Single request with a structured (Pydantic-validated) response."""
    response = client().beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        **_fallback_args(model),
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Structured response was truncated (max_tokens)")
    if response.parsed_output is None:
        raise RuntimeError("Model returned no structured output")
    return response.parsed_output

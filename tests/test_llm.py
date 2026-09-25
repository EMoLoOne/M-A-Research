from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from digest import llm, main, research
from digest.config import load_config

NOW = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


def _api_error(cls, status):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=req), body=None)


def test_fallback_moves_to_next_model_when_unavailable():
    calls = []

    def call(model):
        calls.append(model)
        if model == "a":
            raise _api_error(anthropic.NotFoundError, 404)
        return "ok"

    assert llm.with_model_fallback(["a", "b"], call, "t") == "ok"
    assert calls == ["a", "b"]  # a 404 model is not retried


def test_output_errors_retry_same_model_then_fall_back():
    calls = []

    def call(model):
        calls.append(model)
        if model == "a":
            raise llm.ModelOutputError("truncated")
        return "ok"

    assert llm.with_model_fallback(["a", "b"], call, "t") == "ok"
    assert calls == ["a", "a", "b"]


def test_auth_errors_are_not_retried():
    calls = []

    def call(model):
        calls.append(model)
        raise _api_error(anthropic.AuthenticationError, 401)

    with pytest.raises(llm.ConfigurationError):
        llm.with_model_fallback(["a", "b"], call, "t")
    assert calls == ["a"]


def test_all_models_failing_raises_with_context():
    def call(model):
        raise llm.ModelOutputError("refused")

    with pytest.raises(RuntimeError, match="failed on all models"):
        llm.with_model_fallback(["a", "b"], call, "t")


class Out(BaseModel):
    x: int


def _stream_returning(*responses):
    it = iter(responses)
    ctx = mock.MagicMock()
    ctx.__enter__.return_value.get_final_message.side_effect = lambda: next(it)
    return ctx


def _resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, stop_details=None, content=[SimpleNamespace(type="text", text=text)])


def test_parse_falls_back_on_truncation_and_skips_fallback_param_for_sonnet(monkeypatch):
    client = mock.MagicMock()
    client.beta.messages.stream.side_effect = [
        _stream_returning(_resp('{"x":', "max_tokens")),
        _stream_returning(_resp('{"x":', "max_tokens")),
        _stream_returning(_resp('{"x": 3}')),
    ]
    monkeypatch.setattr(llm, "_client", client)
    assert llm.parse(models=["claude-sonnet-5", "claude-opus-5"], system="s", prompt="p", schema=Out).x == 3
    kwargs = [c.kwargs for c in client.beta.messages.stream.call_args_list]
    assert [k["model"] for k in kwargs] == ["claude-sonnet-5", "claude-sonnet-5", "claude-opus-5"]
    assert "fallbacks" not in kwargs[0] and kwargs[2]["fallbacks"] == "default"
    assert all(k["max_tokens"] == llm.MAX_TOKENS for k in kwargs)


def test_research_all_raises_when_every_sector_fails(monkeypatch):
    cfg = load_config()
    monkeypatch.setattr(llm, "run_with_tools", mock.Mock(side_effect=RuntimeError("down")))
    with pytest.raises(research.ResearchError):
        research.research_all(cfg, "", NOW, NOW, "2026-09-25")


def test_research_all_marks_partial_failures(monkeypatch):
    cfg = load_config()

    def fake(**kw):
        if "Pets" in kw["what"]:
            raise RuntimeError("down")
        return "notes"

    monkeypatch.setattr(llm, "run_with_tools", fake)
    notes, failed = research.research_all(cfg, "", NOW, NOW, "2026-09-25")
    assert failed == ["Pets"] and "UNAVAILABLE" in notes["Pets"]


def test_preflight_reports_missing_key_and_smtp(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    cfg = load_config()
    cfg.smtp = None
    problems = main.preflight(cfg, dry_run=False)
    assert any("ANTHROPIC_API_KEY" in p for p in problems) and any("SMTP" in p for p in problems)
    assert len(main.preflight(cfg, dry_run=True)) == 1

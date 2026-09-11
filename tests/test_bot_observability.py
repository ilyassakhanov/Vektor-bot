"""Tests for observability wiring in bot.py.

Covers: instrumented LLM/registry composition roots, no message-text leakage
into logs, the default metrics port, and logging/metrics wiring in main().
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import bot
from agent.agent import Agent
from agent.conversation import ConversationManager
from bot import build_llm, build_tool_registry, handle_message
from llm.base import LLM
from llm.instrumented import InstrumentedLLM
from tests.fakes import FakeLLM
from tools.instrumented_registry import InstrumentedToolRegistry
from tools.registry import ToolRegistry

_DENIAL_MESSAGE = "Sorry, you are not allowed to use this bot."


def _make_message(text: str, username: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=1, username=username, first_name="Tester"),
        text=text,
    )


def _make_conv() -> ConversationManager:
    return ConversationManager(Agent(FakeLLM(reply="ok"), ToolRegistry()))


# --- Instrumented composition roots ------------------------------------------


def test_build_llm_returns_instrumented():
    llm = build_llm()
    try:
        assert isinstance(llm, InstrumentedLLM)
        assert isinstance(llm, LLM)
    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()


def test_build_tool_registry_returns_instrumented():
    reg = build_tool_registry()
    assert isinstance(reg, InstrumentedToolRegistry)
    names = {spec.name for spec in reg.specs()}
    assert "exec" in names
    assert "get_latest_cve" in names


# --- No message text in logs --------------------------------------------------


def test_handle_message_does_not_log_message_text(caplog):
    caplog.set_level(logging.INFO)
    reply: dict[str, str] = {}
    handle_message(
        _make_message("SUPERSECRETCONTENT"),
        _make_conv(),
        lambda _msg, text: reply.__setitem__("text", text),
    )
    assert reply.get("text") == "ok"
    assert caplog.records
    for record in caplog.records:
        assert "SUPERSECRETCONTENT" not in record.getMessage()
    assert any("42" in record.getMessage() for record in caplog.records)


def test_handle_message_denied_user_no_text_leak(caplog):
    caplog.set_level(logging.INFO)
    reply: dict[str, str] = {}
    handle_message(
        _make_message("SECRETTEXT", username="intruder"),
        _make_conv(),
        lambda _msg, text: reply.__setitem__("text", text),
        frozenset({"someone"}),
    )
    assert reply.get("text") == _DENIAL_MESSAGE
    for record in caplog.records:
        assert "SECRETTEXT" not in record.getMessage()


# --- main() wiring -----------------------------------------------------------


def test_default_metrics_port_constant():
    assert bot._DEFAULT_METRICS_PORT == 9100


def test_metrics_port_from_env_valid(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "9201")
    assert bot._metrics_port_from_env() == 9201


def test_metrics_port_from_env_invalid_returns_default(monkeypatch, caplog):
    monkeypatch.setenv("METRICS_PORT", "not-a-port")
    with caplog.at_level(logging.WARNING):
        assert bot._metrics_port_from_env() == bot._DEFAULT_METRICS_PORT
    assert "Invalid METRICS_PORT" in caplog.text


def test_metrics_port_from_env_unset_returns_default(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    assert bot._metrics_port_from_env() == bot._DEFAULT_METRICS_PORT


def test_main_wiring_exists():
    source = Path(bot.__file__).read_text()
    main_start = source.index("def main()")
    module_level, main_body = source[:main_start], source[main_start:]
    assert "configure_logging(" in main_body
    assert "start_metrics_server(" in main_body
    assert "start_metrics_server(" not in module_level
    assert "basicConfig" not in module_level

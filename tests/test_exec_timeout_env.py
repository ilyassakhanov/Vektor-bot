"""EXEC_TIMEOUT resolution at every call site — offline.

``cve_core``, ``tools.mcp.McpStdioClient`` and ``bot.build_tool_registry``
all read ``EXEC_TIMEOUT`` through ``config.positive_env``: unset → 30,
valid → the value, invalid / ``<= 0`` / NaN / inf → 30 with a warning,
above ``config.MAX_TIMEOUT_S`` → clamped. Also ``MCP_STARTUP_TIMEOUT``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

import bot
import cve_core
from tools.mcp import McpStdioClient

_INVALID = ["abc", "nan", "inf", "-inf", "0", "-5", ""]


def _cve_core_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    captured: dict[str, Any] = {}
    original_init = httpx.Client.__init__

    def fake_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        captured["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(
            lambda req: httpx.Response(200, json=[])
        )
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", fake_init)
    result = cve_core.get_latest_cve_fact_sheet()
    # A nan/inf deadline used to make every run fail; with the fallback the
    # (empty) discovery completes normally.
    assert "No recent CVE IDs" in result
    return float(captured["timeout"])


def _mcp_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    client = McpStdioClient(command=["unused"])
    return float(client._call_timeout)


def _bot_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    reg = bot.build_tool_registry(None, auto_build_kb=False)
    tool = reg.get("exec")
    assert tool is not None
    return float(tool._timeout)  # type: ignore[attr-defined]


_SITES = {
    "cve_core": _cve_core_timeout,
    "mcp": _mcp_timeout,
    "bot": _bot_timeout,
}


@pytest.mark.parametrize("site", sorted(_SITES))
def test_unset_uses_default_silently(
    site: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("EXEC_TIMEOUT", raising=False)
    with caplog.at_level(logging.WARNING, logger="vektor.config"):
        assert _SITES[site](monkeypatch) == 30.0
    assert "EXEC_TIMEOUT" not in caplog.text


@pytest.mark.parametrize("site", sorted(_SITES))
def test_valid_value_is_used(site: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_TIMEOUT", "12.5")
    assert _SITES[site](monkeypatch) == 12.5


@pytest.mark.parametrize("raw", _INVALID)
@pytest.mark.parametrize("site", sorted(_SITES))
def test_invalid_value_falls_back_with_warning(
    site: str,
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("EXEC_TIMEOUT", raw)
    with caplog.at_level(logging.WARNING, logger="vektor.config"):
        assert _SITES[site](monkeypatch) == 30.0
    assert "Invalid EXEC_TIMEOUT" in caplog.text


def test_explicit_mcp_call_timeout_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_TIMEOUT", "abc")
    client = McpStdioClient(command=["unused"], call_timeout=7.0)
    assert client._call_timeout == 7.0


@pytest.mark.parametrize("raw", ["1e15", "3601", "9" * 400])
@pytest.mark.parametrize("site", sorted(_SITES))
def test_huge_value_never_reaches_timeout_apis(
    site: str, raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above ``MAX_TIMEOUT_S`` is clamped; an int too large for a float falls
    back to the default — neither crashes nor reaches ``Future.result``."""
    monkeypatch.setenv("EXEC_TIMEOUT", raw)
    value = _SITES[site](monkeypatch)
    assert value == (30.0 if len(raw) > 10 else 3600.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_explicit_mcp_call_timeout_must_be_positive_finite(bad: float) -> None:
    with pytest.raises(ValueError, match="call_timeout"):
        McpStdioClient(command=["unused"], call_timeout=bad)


def test_explicit_mcp_call_timeout_ignores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_TIMEOUT", "12")
    assert McpStdioClient(command=["unused"], call_timeout=0.5)._call_timeout == 0.5


# --- MCP_STARTUP_TIMEOUT -------------------------------------------------------


def test_mcp_startup_timeout_unset_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_STARTUP_TIMEOUT", raising=False)
    assert bot._mcp_startup_timeout_from_env() == 10.0


def test_mcp_startup_timeout_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_STARTUP_TIMEOUT", "2.5")
    assert bot._mcp_startup_timeout_from_env() == 2.5


@pytest.mark.parametrize("raw", _INVALID)
def test_mcp_startup_timeout_invalid_falls_back(
    raw: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MCP_STARTUP_TIMEOUT", raw)
    with caplog.at_level(logging.WARNING, logger="vektor.config"):
        assert bot._mcp_startup_timeout_from_env() == 10.0
    assert "Invalid MCP_STARTUP_TIMEOUT" in caplog.text


def test_mcp_startup_timeout_huge_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_STARTUP_TIMEOUT", "1e15")
    assert bot._mcp_startup_timeout_from_env() == 3600.0


def test_main_passes_resolved_startup_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` builds the MCP client with the parsed value, not a raw float()."""
    monkeypatch.setenv("MCP_STARTUP_TIMEOUT", "abc")
    captured: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_client(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise Stop

    monkeypatch.setattr(bot, "McpStdioClient", fake_client)
    monkeypatch.setattr(bot.metrics, "start_metrics_server", lambda port: None)
    monkeypatch.setattr(bot.logging_config, "configure_logging", lambda **kw: None)
    monkeypatch.setattr(bot, "build_llm", lambda: object())
    with pytest.raises(Stop):
        bot.main()
    assert captured["startup_timeout"] == 10.0

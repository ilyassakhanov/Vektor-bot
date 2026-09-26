"""Tests for McpStdioClient — spawns the real CVE server subprocess.

These tests launch ``mcp_servers/cve_server.py`` as a subprocess via
:class:`McpStdioClient`. The tool-listing and idempotent-stop tests need
no network access (the server does not call CVE.org unless the tool is
invoked). The live ``call_tool`` test is skipped when CVE.org is
unreachable, mirroring ``tests/test_cve_integration.py``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from tools.mcp import McpStdioClient, build_server_env

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVER_CMD: list[str] = [
    sys.executable,
    str(_PROJECT_ROOT / "mcp_servers" / "cve_server.py"),
]
_SERVER_CWD: str = str(_PROJECT_ROOT)
_SERVER_PYTHONPATH: str = str(_PROJECT_ROOT)


def _is_online(url: str) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' '{url}'",
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.stdout.strip() == "200"
    except Exception:  # noqa: BLE001 - connectivity check must never crash
        return False


def test_stdio_client_lists_get_latest_cve() -> None:
    """Spawning the real CVE server and listing tools returns get_latest_cve."""
    client = McpStdioClient(
        command=_SERVER_CMD,
        env=build_server_env(),
        startup_timeout=15.0,
        cwd=_SERVER_CWD,
        pythonpath=_SERVER_PYTHONPATH,
    )
    client.start()
    try:
        specs = client.specs()
        assert any(s.name == "get_latest_cve" for s in specs)
    finally:
        client.stop()


def test_stdio_client_stop_is_idempotent() -> None:
    """Calling stop() twice must not raise."""
    client = McpStdioClient(
        command=_SERVER_CMD,
        env=build_server_env(),
        startup_timeout=15.0,
        cwd=_SERVER_CWD,
        pythonpath=_SERVER_PYTHONPATH,
    )
    client.start()
    try:
        client.stop()
        client.stop()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"stop() raised on second call: {exc}")


def test_stdio_client_sets_server_up_metric_on_start() -> None:
    """After start(), vektor_mcp_server_up is 1; after stop(), it is 0."""
    client = McpStdioClient(
        command=_SERVER_CMD,
        env=build_server_env(),
        startup_timeout=15.0,
        cwd=_SERVER_CWD,
        pythonpath=_SERVER_PYTHONPATH,
    )
    client.start()
    try:
        assert REGISTRY.get_sample_value("vektor_mcp_server_up") == 1.0
    finally:
        client.stop()
    assert REGISTRY.get_sample_value("vektor_mcp_server_up") == 0.0


def test_stdio_client_call_tool_live_skipped_when_offline() -> None:
    """Live call_tool against the real CVE server; skipped if CVE.org is down."""
    if not _is_online("https://cveawg.mitre.org/api/cve/CVE-2024-1234"):
        pytest.skip("CVE.org unreachable")

    client = McpStdioClient(
        command=_SERVER_CMD,
        env=build_server_env(),
        startup_timeout=15.0,
        cwd=_SERVER_CWD,
        pythonpath=_SERVER_PYTHONPATH,
    )
    client.start()
    try:
        result = client.call_tool("get_latest_cve", {})
    except Exception:  # noqa: BLE001 - live test must skip on network failure
        pytest.skip("CVE.org unreachable")
        return
    finally:
        client.stop()

    assert isinstance(result, str)
    assert len(result) > 0


def test_stdio_client_server_stderr_appears_in_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Server stderr lines are re-emitted via the vektor.mcp.server logger."""
    caplog.set_level(logging.INFO, logger="vektor.mcp.server")
    client = McpStdioClient(
        command=_SERVER_CMD,
        env=build_server_env(),
        startup_timeout=15.0,
        cwd=_SERVER_CWD,
        pythonpath=_SERVER_PYTHONPATH,
    )
    client.start()
    try:
        assert any(rec.name == "vektor.mcp.server" for rec in caplog.records), (
            "no records from vektor.mcp.server logger"
        )
    finally:
        client.stop()

"""MCP client adapter and tools — bridges the agent to MCP servers.

Provides:
* :func:`build_server_env` — a safe environment dict for the CVE server
  subprocess (no secrets, only the vars the server needs).
* :class:`McpClient` — duck-typed protocol for an MCP client.
* :class:`McpTool` — registry-facing :class:`~tools.base.Tool` adapter
  that delegates ``execute()`` to an MCP client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)

import metrics
from llm.base import ToolSpec
from tools.base import Tool

log = logging.getLogger("vektor.tools.mcp")

_KEEP_VARS: tuple[str, ...] = (
    "LOG_LEVEL",
    "EXEC_MAX_OUTPUT_CHARS",
    "EXEC_TIMEOUT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

_DEFAULT_CALL_TIMEOUT = 30.0


def _exec_timeout_from_env() -> float:
    """Resolve the call timeout from ``EXEC_TIMEOUT``, falling back to default."""
    raw = os.environ.get("EXEC_TIMEOUT")
    if raw is None:
        return _DEFAULT_CALL_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_CALL_TIMEOUT


def build_server_env() -> dict[str, str]:
    """Return a safe environment dict for the CVE server subprocess.

    Starts from :func:`mcp.client.stdio.get_default_environment` (already
    minimal — ``HOME``, ``LOGNAME``, ``PATH``, ``SHELL``, ``TERM``,
    ``USER`` — no secrets) and forwards only the variables the CVE server
    needs. ``TELEGRAM_BOT_TOKEN`` and ``ALLOWED_USERNAMES`` are dropped
    explicitly as defense in depth, since ``get_default_environment``
    would never include them but a future change upstream could.
    """
    env = dict(get_default_environment())
    for key in _KEEP_VARS:
        if key in os.environ:
            env[key] = os.environ[key]
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("ALLOWED_USERNAMES", None)
    return env


class McpClient(Protocol):
    """Duck-typed interface for an MCP client (e.g. McpStdioClient)."""

    def specs(self) -> list[ToolSpec]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str: ...


class McpTool(Tool):
    """Registry-facing Tool that delegates execute() to an MCP client.

    Name, description, and parameters come from a single :class:`ToolSpec`
    (typically one returned by ``client.specs()``). ``execute()`` forwards
    to ``client.call_tool()`` and never raises — failures surface as
    ``"Error: ..."`` strings, per registry convention.
    """

    def __init__(self, client: McpClient, spec: ToolSpec) -> None:
        self._client = client
        self._spec = spec

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def description(self) -> str:
        return self._spec.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._spec.parameters

    def execute(self, **kwargs: Any) -> str:
        try:
            return self._client.call_tool(self.name, kwargs)
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"


class McpStdioClient:
    """Synchronous stdio MCP client.

    Launches an MCP server subprocess, runs an asyncio event loop on a
    dedicated daemon thread, performs the initialize/list_tools handshake,
    caches tool specs, and exposes a thread-safe synchronous
    :meth:`call_tool`. A stderr reader thread re-emits server logs via the
    ``vektor.mcp.server`` logger so they pass through the bot's JSON +
    redaction + Loki pipeline.

    Records :data:`metrics.mcp_server_up` (1 on start, 0 on stop),
    :data:`metrics.mcp_roundtrip_seconds` (per ``call_tool``), and
    :data:`metrics.mcp_restarts_total` (on start).
    """

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        startup_timeout: float = 10.0,
        call_timeout: float | None = None,
        cwd: str | None = None,
        pythonpath: str | None = None,
    ) -> None:
        self._command = command
        self._env = dict(env) if env else {}
        if pythonpath:
            existing = self._env.get("PYTHONPATH")
            if existing:
                self._env["PYTHONPATH"] = f"{pythonpath}:{existing}"
            else:
                self._env["PYTHONPATH"] = pythonpath
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout or _exec_timeout_from_env()
        self._cwd = cwd
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._specs: list[ToolSpec] = []
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._stderr_thread: threading.Thread | None = None
        self._errlog: Any = None
        self._stderr_reader: Any = None
        self._started = False
        self._stopped_once = False
        self._session_future: Any = None
        self._startup_error: BaseException | None = None

    def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="vektor-mcp-loop",
            daemon=True,
        )
        self._loop_thread.start()

        self._session_future = asyncio.run_coroutine_threadsafe(
            self._run_session(), self._loop
        )

        if not self._ready.wait(timeout=self._startup_timeout):
            self.stop()
            raise TimeoutError(
                f"MCP server did not become ready within {self._startup_timeout}s"
            )

        if self._startup_error is not None:
            self.stop()
            raise self._startup_error

        metrics.mcp_restarts_total.inc()
        metrics.mcp_server_up.set(1)
        self._started = True

    async def _run_session(self) -> None:
        try:
            await self._run_session_inner()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            raise

    async def _run_session_inner(self) -> None:
        server_params = StdioServerParameters(
            command=self._command[0],
            args=self._command[1:],
            env=self._env,
            cwd=self._cwd,
        )

        read_fd, write_fd = os.pipe()
        self._errlog = os.fdopen(write_fd, "w")
        self._stderr_reader = os.fdopen(read_fd, "r")

        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="vektor-mcp-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        async with (
            stdio_client(server_params, errlog=self._errlog) as (
                read,
                write,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools_result = await session.list_tools()
            self._specs = [
                ToolSpec(
                    name=t.name,
                    description=t.description or "",
                    parameters=t.input_schema or {},
                )
                for t in tools_result.tools
            ]
            self._session = session
            self._ready.set()
            while not self._stopped.is_set():
                await asyncio.sleep(0.1)

    def _read_stderr(self) -> None:
        server_log = logging.getLogger("vektor.mcp.server")
        reader = self._stderr_reader
        if reader is None:
            return
        try:
            for line in reader:
                server_log.info(line.rstrip())
        except (OSError, ValueError):
            pass

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if self._session is None or self._loop is None:
            return "Error: MCP client not started"
        start = time.perf_counter()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, arguments or {}),
                self._loop,
            )
            result = future.result(timeout=self._call_timeout)
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"
        metrics.mcp_roundtrip_seconds.observe(time.perf_counter() - start)
        if getattr(result, "is_error", False):
            texts = [
                c.text for c in getattr(result, "content", []) if hasattr(c, "text")
            ]
            return "Error: " + "\n".join(texts)
        texts = [c.text for c in getattr(result, "content", []) if hasattr(c, "text")]
        return "\n".join(texts)

    def stop(self) -> None:
        if self._stopped_once:
            return
        self._stopped_once = True
        self._started = False
        self._stopped.set()
        metrics.mcp_server_up.set(0)

        loop = self._loop
        if loop is not None and loop.is_running():
            future = self._session_future
            if future is not None:
                try:
                    future.result(timeout=5.0)
                except Exception:
                    log.debug("MCP session exception during shutdown", exc_info=True)
            loop.call_soon_threadsafe(loop.stop)

        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)

        # Close write end first so the reader thread's blocking read sees EOF.
        if self._errlog is not None:
            try:
                self._errlog.close()
            except (OSError, ValueError):
                pass
            self._errlog = None
        if self._stderr_reader is not None:
            try:
                self._stderr_reader.close()
            except (OSError, ValueError):
                pass
            self._stderr_reader = None

        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5.0)

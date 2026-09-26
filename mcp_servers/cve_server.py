"""MCP server exposing get_latest_cve backed by cve_core.

Uses mcp 2.x (MCPServer). All logging to stderr; stdout is the JSON-RPC
protocol channel and must never carry log lines.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.mcpserver import MCPServer

import cve_core

log = logging.getLogger("vektor.mcp.cve_server")

_DESCRIPTION = (
    "Get the most critical recent CVE (highest CVSS) from official "
    "CVE.org data. Use for latest or most severe CVE questions."
)


def _get_latest_cve() -> str:
    return cve_core.get_latest_cve_fact_sheet()


def build_server(server: MCPServer | None = None) -> MCPServer:
    srv = server if server is not None else MCPServer(name="cve")
    srv.add_tool(_get_latest_cve, name="get_latest_cve", description=_DESCRIPTION)
    return srv


def main() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    log.info("CVE MCP server starting (stdio transport)")
    server = build_server()
    server.run("stdio")


if __name__ == "__main__":
    main()

"""Shared Ollama probe for gated test suites — skip when Ollama is down.

Hoists the ``benchmarks/run.py::ollama_reachable()`` probe pattern into a
standalone test helper: one GET to ``{base}/api/tags`` with a 2 s timeout,
reachable when the status is below 500, unreachable on any ``httpx`` error.
Import-side-effect free — nothing is probed at import time; gated suites
call :func:`ollama_reachable` from an autouse ``pytest.skip`` fixture (the
``tests/test_cve_integration.py`` gating idiom).

See openspec/changes/2026-10-22-agent-system-prompt/ (task 2.2).
"""

from __future__ import annotations

import functools
import logging
import os

import httpx

log = logging.getLogger("vektor.tests.ollama_probe")

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2"
_PROBE_TIMEOUT = 2.0


def ollama_base_url() -> str:
    """Ollama HTTP base URL from the environment (default localhost:11434)."""
    return os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URL)


def ollama_model() -> str:
    """Ollama model name from the environment (default llama3.2)."""
    return os.environ.get("OLLAMA_MODEL", _DEFAULT_MODEL)


@functools.lru_cache(maxsize=1)
def ollama_reachable() -> bool:
    """Probe the Ollama instance — True when it answers with a sane status.

    Cached so a pytest process probes at most once (parametrized red-team
    cases must not re-probe); the environment is stable within a run.
    """
    base_url = ollama_base_url()
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=_PROBE_TIMEOUT)
    except httpx.HTTPError as exc:
        log.info("Ollama probe failed for %s: %s", base_url, exc)
        return False
    return resp.status_code < 500

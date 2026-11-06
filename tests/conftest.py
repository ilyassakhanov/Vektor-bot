"""Pytest configuration — env defaults for importing bot.py without secrets.

KB_ENABLED is FORCED to "0" (not setdefault) so composition-root tests
(build_tool_registry with no kb argument) never auto-build a real
knowledge-base stack against the default data/vektor.db path — a developer
or CI environment exporting KB_ENABLED=1 must not leak into the suite and
add tools tests do not expect. kb-on tests override this via monkeypatch
(delenv/setenv), which wins over the assignment here.

Tests marked ``live`` (red-team, judge, memory, latency — real Ollama,
nondeterministic, hardware-dependent) are opt-in: they are skipped unless
``VEKTOR_LIVE_TESTS=1``, so a plain ``python -m pytest`` stays offline even
when Ollama is running. Run them with
``VEKTOR_LIVE_TESTS=1 python -m pytest -m live``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:dummy-token-for-tests")
os.environ["KB_ENABLED"] = "0"


_LIVE_ENV = "VEKTOR_LIVE_TESTS"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get(_LIVE_ENV) == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live suite; set {_LIVE_ENV}=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)

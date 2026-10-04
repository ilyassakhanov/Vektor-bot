"""Pytest configuration — env defaults for importing bot.py without secrets.

KB_ENABLED is FORCED to "0" (not setdefault) so composition-root tests
(build_tool_registry with no kb argument) never auto-build a real
knowledge-base stack against the default data/vektor.db path — a developer
or CI environment exporting KB_ENABLED=1 must not leak into the suite and
add tools tests do not expect. kb-on tests override this via monkeypatch
(delenv/setenv), which wins over the assignment here.
"""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:dummy-token-for-tests")
os.environ["KB_ENABLED"] = "0"

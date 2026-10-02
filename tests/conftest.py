"""Pytest configuration — env defaults for importing bot.py without secrets.

KB_ENABLED defaults to "0" so composition-root tests (build_tool_registry
with no kb argument) never auto-build a real knowledge-base stack against
the default data/vektor.db path; kb-on tests override this via monkeypatch
(delenv/setenv), which always wins over the setdefault here.
"""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:dummy-token-for-tests")
os.environ.setdefault("KB_ENABLED", "0")

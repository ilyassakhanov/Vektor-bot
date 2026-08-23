"""Pytest configuration — ensure bot.py can be imported without real secrets."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:dummy-token-for-tests")

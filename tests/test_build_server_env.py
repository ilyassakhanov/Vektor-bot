"""Tests for ``build_server_env`` — safe env dict for the CVE server subprocess."""

from __future__ import annotations

from tools.mcp import build_server_env


def test_build_server_env_excludes_telegram_bot_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token")
    env = build_server_env()
    assert "TELEGRAM_BOT_TOKEN" not in env


def test_build_server_env_excludes_allowed_usernames(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERNAMES", "@alice,@bob")
    env = build_server_env()
    assert "ALLOWED_USERNAMES" not in env


def test_build_server_env_includes_log_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    env = build_server_env()
    assert env["LOG_LEVEL"] == "DEBUG"


def test_build_server_env_includes_exec_max_output_chars(monkeypatch):
    monkeypatch.setenv("EXEC_MAX_OUTPUT_CHARS", "1234")
    env = build_server_env()
    assert env["EXEC_MAX_OUTPUT_CHARS"] == "1234"


def test_build_server_env_includes_exec_timeout(monkeypatch):
    monkeypatch.setenv("EXEC_TIMEOUT", "60")
    env = build_server_env()
    assert env["EXEC_TIMEOUT"] == "60"


def test_build_server_env_includes_proxy_vars(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        monkeypatch.setenv(key, "value-for-" + key)
    env = build_server_env()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        assert env[key] == "value-for-" + key


def test_build_server_env_returns_dict_of_strings():
    env = build_server_env()
    assert isinstance(env, dict)
    for value in env.values():
        assert isinstance(value, str)

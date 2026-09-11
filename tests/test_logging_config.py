"""Tests for logging_config — JSON formatter, redaction filter, configure_logging."""

from __future__ import annotations

import json
import logging
from typing import Any

from logging_config import JsonFormatter, RedactionFilter, configure_logging
from loki_handler import LokiPushHandler


def _record(msg: object, args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="vektor.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


# --- JsonFormatter -----------------------------------------------------------


def test_json_formatter_output_keys():
    record = _record("hello %s", ("world",))
    output = JsonFormatter().format(record)
    data = json.loads(output)
    assert set(data.keys()) == {"ts", "level", "logger", "msg"}
    assert data["msg"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "vektor.test"


def test_json_formatter_includes_exc_info():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed", None)
        record.exc_info = sys.exc_info()
    data = json.loads(JsonFormatter().format(record))
    assert "exc_info" in data
    assert "ValueError: boom" in data["exc_info"]


# --- RedactionFilter ---------------------------------------------------------


def test_redaction_filter_redacts_sensitive_kwarg():
    record = _record("text=%r", ("secret message",))
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.msg, str)
    assert isinstance(record.args, tuple)
    assert record.args[-1] == "<redacted>"
    formatted = record.msg % record.args
    assert "<redacted>" in formatted
    assert "secret message" not in formatted


def test_redaction_filter_redacts_message_kwarg():
    record = _record("generate model=%s message=%r", ("llama3.2", "my prompt"))
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[1] == "<redacted>"
    assert record.args[0] == "llama3.2"


def test_redaction_filter_redacts_prompt_kwarg():
    record = _record("prompt=%r", ("p",))
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[0] == "<redacted>"


def test_redaction_filter_redacts_bot_message_site():
    record = _record(
        "message id=%s chat=%s user=%s%s text=%r",
        (1, 42, 7, " @someone", "hello world"),
    )
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[4] == "<redacted>"
    assert record.args[3] == " @someone"
    assert record.args[0] == 1


def test_redaction_filter_preserves_normal_args():
    record = _record("chat=%s user=%s", (42, 1))
    assert RedactionFilter().filter(record) is True
    assert record.args == (42, 1)


def test_redaction_filter_handles_empty_args():
    record = _record("no args here", None)
    assert RedactionFilter().filter(record) is True


def test_redaction_filter_no_args_tuple_dict():
    record = _record("%(k)s", ({"k": "v"},))
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.args, dict)
    assert record.args == {"k": "v"}


def test_redaction_filter_redacts_dict_sensitive_keys():
    record = _record("text=%(text)s", ({"text": "s3cret"},))
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.msg, str)
    assert isinstance(record.args, dict)
    assert record.args["text"] == "<redacted>"
    assert record.msg % record.args == "text=<redacted>"


def test_redaction_filter_handles_width_specs():
    record = _record("text=%10s", ("SECRETTEXT",))
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[0] == "<redacted>"


def test_redaction_filter_handles_precision_specs():
    record = _record("out=%.2f prompt=%r", (1.23, "SECRETPROMPT"))
    assert RedactionFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[0] == 1.23
    assert record.args[1] == "<redacted>"


# --- configure_logging -------------------------------------------------------


def _snapshot_root() -> tuple[list[Any], list[Any], int]:
    root = logging.getLogger()
    return list(root.handlers), list(root.filters), root.level


def _restore_root(snapshot: tuple[list[Any], list[Any], int]) -> None:
    root = logging.getLogger()
    root.handlers = snapshot[0]
    root.filters = snapshot[1]
    root.setLevel(snapshot[2])


def test_configure_logging_sets_up_json_handler():
    snapshot = _snapshot_root()
    try:
        configure_logging(loki_url=None)
        root = logging.getLogger()
        assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
        assert any(isinstance(f, RedactionFilter) for f in root.filters)
        assert any(
            isinstance(f, RedactionFilter) for h in root.handlers for f in h.filters
        )
    finally:
        _restore_root(snapshot)


def test_configure_logging_no_loki_handler_when_none():
    snapshot = _snapshot_root()
    try:
        configure_logging(loki_url=None)
        root = logging.getLogger()
        for handler in root.handlers:
            assert type(handler).__name__ != "LokiPushHandler"
            assert not hasattr(handler, "loki_url")
    finally:
        _restore_root(snapshot)


def test_configure_logging_installs_loki_handler_when_url_set():
    snapshot = _snapshot_root()
    try:
        configure_logging(loki_url="http://loki.example/loki/api/v1/push")
        root = logging.getLogger()
        assert any(isinstance(h, LokiPushHandler) for h in root.handlers)
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, LokiPushHandler):
                handler.close()
        _restore_root(snapshot)

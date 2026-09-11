"""Tests for LokiPushHandler — buffered Loki push via httpx MockTransport."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

from logging_config import JsonFormatter
from loki_handler import LokiPushHandler

PUSH_URL = "http://loki:3100/loki/api/v1/push"


def _record(msg: str, *args: Any) -> logging.LogRecord:
    return logging.LogRecord(
        name="vektor.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or None,
        exc_info=None,
    )


def _mock_client(
    calls: list[httpx.Request], response: httpx.Response | Exception
) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


# --- buffering ---------------------------------------------------------------


def test_emit_buffers_records():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.Response(204))
    )
    handler.emit(_record("one"))
    handler.emit(_record("two"))
    assert handler.pending() == 2
    assert calls == []


def test_emit_skips_own_logger_records():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.Response(204))
    )
    own = logging.LogRecord(
        name="vektor.loki_handler",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Loki push failed",
        args=None,
        exc_info=None,
    )
    handler.emit(own)
    assert handler.pending() == 0


# --- flush -------------------------------------------------------------------


def test_flush_posts_to_loki():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.Response(204))
    )
    handler.setFormatter(JsonFormatter())
    handler.emit(_record("one"))
    handler.emit(_record("two"))
    handler.emit(_record("three"))
    handler.flush()
    assert len(calls) == 1
    req = calls[0]
    assert req.method == "POST"
    assert str(req.url) == PUSH_URL
    body = json.loads(req.content)
    assert isinstance(body["streams"], list)
    assert len(body["streams"]) == 1
    stream = body["streams"][0]
    assert stream["stream"]["job"] == "vektor"
    values = stream["values"]
    assert len(values) == 3
    for ts, line in values:
        assert isinstance(ts, str)
        line_data = json.loads(line)
        assert line_data["msg"] in {"one", "two", "three"}
    assert handler.pending() == 0


def test_flush_noop_when_url_none():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(url=None, client=_mock_client(calls, httpx.Response(204)))
    handler.emit(_record("one"))
    handler.flush()
    assert calls == []
    assert handler.pending() == 0


def test_flush_noop_when_buffer_empty():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.Response(204))
    )
    handler.flush()
    assert calls == []


def test_flush_drops_batch_on_failure(caplog):
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.ConnectError("refused"))
    )
    handler.emit(_record("one"))
    handler.emit(_record("two"))
    with caplog.at_level(logging.WARNING, logger="vektor.loki_handler"):
        handler.flush()
    assert handler.pending() == 0
    assert "Loki push failed" in caplog.text


# --- runtime flushing ---------------------------------------------------------


def test_periodic_flush_thread_pushes():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL,
        client=_mock_client(calls, httpx.Response(204)),
        flush_interval=0.05,
    )
    try:
        handler.emit(_record("one"))
        time.sleep(0.3)
        assert len(calls) >= 1
    finally:
        handler.close()


def test_size_threshold_triggers_flush():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL,
        client=_mock_client(calls, httpx.Response(204)),
        flush_threshold=3,
    )
    try:
        handler.emit(_record("one"))
        handler.emit(_record("two"))
        assert calls == []
        handler.emit(_record("three"))
        assert len(calls) == 1
        assert handler.pending() == 0
    finally:
        handler.close()


def test_close_is_idempotent():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.Response(204))
    )
    handler.emit(_record("one"))
    handler.close()
    handler.close()
    assert len(calls) == 1


def test_flush_thread_safe_under_concurrent_emit():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL,
        client=_mock_client(calls, httpx.Response(204)),
        flush_threshold=1000,
    )
    stop = threading.Event()

    def flusher() -> None:
        while not stop.is_set():
            handler.flush()
            time.sleep(0.001)

    t = threading.Thread(target=flusher)
    t.start()
    try:
        for i in range(50):
            handler.emit(_record(f"line {i}"))
    finally:
        stop.set()
        t.join()
    handler.flush()
    assert handler.pending() == 0
    posted = sum(len(json.loads(req.content)["streams"][0]["values"]) for req in calls)
    assert posted == 50
    handler.close()


# --- close -------------------------------------------------------------------


def test_close_flushes_remaining():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.Response(204))
    )
    handler.emit(_record("one"))
    handler.close()
    assert len(calls) == 1


def test_close_no_crash_when_url_none():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(url=None, client=_mock_client(calls, httpx.Response(204)))
    handler.emit(_record("one"))
    handler.close()
    assert calls == []


# --- timestamps --------------------------------------------------------------


def test_timestamps_are_nanoseconds():
    calls: list[httpx.Request] = []
    handler = LokiPushHandler(
        url=PUSH_URL, client=_mock_client(calls, httpx.Response(204))
    )
    handler.emit(_record("one"))
    handler.flush()
    body = json.loads(calls[0].content)
    ts = body["streams"][0]["values"][0][0]
    assert ts.isdigit()
    assert 18 <= len(ts) <= 20

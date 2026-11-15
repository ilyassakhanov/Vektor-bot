"""Tests for cve_core — fetches recent CVE records and selects the most critical.

All tests use httpx.MockTransport — no network access needed.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from cve_core import get_latest_cve_fact_sheet


def _make_cve(
    cve_id: str,
    date_published: str,
    score: float | None = None,
    severity: str | None = None,
    description: str = "A vulnerability",
    vendor: str = "vendor",
    product: str = "product",
    vector: str = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
) -> dict[str, Any]:
    metrics: list[dict[str, Any]] = []
    if score is not None:
        metrics.append(
            {
                "cvssV3_1": {
                    "version": "3.1",
                    "vectorString": vector,
                    "baseScore": score,
                    "baseSeverity": severity or "HIGH",
                }
            }
        )
    return {
        "dataType": "CVE_RECORD",
        "dataVersion": "5.2",
        "cveMetadata": {
            "cveId": cve_id,
            "state": "PUBLISHED",
            "datePublished": date_published,
            "dateUpdated": date_published,
        },
        "containers": {
            "cna": {
                "providerMetadata": {"shortName": "test-cna"},
                "affected": [
                    {
                        "vendor": vendor,
                        "product": product,
                        "versions": [{"version": "1.0", "status": "affected"}],
                    }
                ],
                "descriptions": [{"lang": "en", "value": description}],
                "metrics": metrics,
            }
        },
    }


_COMMIT_MSG_WITH_CVES = (
    "3 changes (2 new | 1 updated):\n"
    "  - 2 new CVEs:  CVE-2026-0001, CVE-2026-0002\n"
    "  - 1 updated CVEs: CVE-2026-0001\n"
)


def _commits_response(cve_ids: list[str]) -> list[dict[str, Any]]:
    msg = f"1 new CVEs:  {', '.join(cve_ids)}\n"
    return [{"commit": {"message": msg}}]


def _make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


# --- Happy path: returns selected CVE fact sheet ----------------------------


def test_returns_highest_scoring_cve_from_latest_window():
    """Two CVEs in the same window — the higher score wins."""
    cve_low = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=5.0)
    cve_high = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=9.8)
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])
    records = {c["cveMetadata"]["cveId"]: c for c in (cve_low, cve_high)}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=records[cve_id])

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "CVE-2026-0002" in result
    assert "9.8" in result
    assert "CVE-2026-0001" not in result.split("\n")[0]


def test_result_contains_all_fields():
    cve = _make_cve(
        "CVE-2026-0001",
        "2026-08-28T15:23:00Z",
        score=9.8,
        severity="CRITICAL",
        description="Buffer overflow in product X",
        vendor="acme",
        product="widget",
        vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    )
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=cve)

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "CVE_ID: CVE-2026-0001" in result
    assert "CVSS_SCORE: 9.8" in result
    assert "CVSS_SEVERITY: CRITICAL" in result
    assert "PUBLISHED: 2026-08-28T15:23:00Z" in result
    assert "VENDOR: acme" in result
    assert "PRODUCT: widget" in result
    assert "Buffer overflow" in result
    assert "ATTACK_VECTOR" in result
    assert "DATA_SOURCE: CVE.org" in result
    assert "RECORDS_RETRIEVED: 1" in result


def test_older_high_score_loses_to_newer_window():
    """A globally higher score from an older window must not win."""
    older_high = _make_cve("CVE-2026-0001", "2026-08-28T07:30:00Z", score=9.9)
    newer_low = _make_cve("CVE-2026-0002", "2026-08-28T15:23:00Z", score=5.0)
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])
    records = {c["cveMetadata"]["cveId"]: c for c in (older_high, newer_low)}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=records[cve_id])

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "CVE-2026-0002" in result
    assert "CVE-2026-0001" not in result.split("\n")[0]


def test_equal_scores_tiebreak_by_most_recent():
    cve_a = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=9.8)
    cve_b = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=9.8)
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])
    records = {c["cveMetadata"]["cveId"]: c for c in (cve_a, cve_b)}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=records[cve_id])

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "CVE-2026-0002" in result


# --- Missing CVSS ----------------------------------------------------------


def test_all_missing_cvss_returns_message():
    cve_a = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None)
    cve_b = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=None)
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])
    records = {c["cveMetadata"]["cveId"]: c for c in (cve_a, cve_b)}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=records[cve_id])

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "none" in result.lower() or "no" in result.lower()
    assert "CVSS" in result


def test_missing_cvss_cannot_win():
    cve_no_score = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None)
    cve_with_score = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=5.0)
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])
    records = {c["cveMetadata"]["cveId"]: c for c in (cve_no_score, cve_with_score)}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=records[cve_id])

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "CVE-2026-0002" in result


# --- Network errors --------------------------------------------------------


def test_github_api_failure_returns_error_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "No recent CVE IDs" in result or "failed" in result.lower()


def test_cve_record_fetch_failure_returns_partial_result():
    """If some records fail to fetch, the tool still returns the ones it got."""
    cve_good = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.5)
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        if cve_id == "CVE-2026-0002":
            return httpx.Response(404)
        return httpx.Response(200, json=cve_good)

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "CVE-2026-0001" in result


def test_cve_record_malformed_json_skipped():
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, content=b"not-json")

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "Failed to retrieve" in result or "none" in result.lower()


def test_commits_malformed_json_returns_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "No recent CVE IDs" in result or "failed" in result.lower()


# --- Commit parsing --------------------------------------------------------


def test_multiple_commits_cve_ids_aggregated():
    """CVE IDs from multiple commits are all collected."""
    cve_a = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    cve_b = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=9.0)
    commits = [
        {"commit": {"message": "1 new CVEs:  CVE-2026-0001\n"}},
        {"commit": {"message": "1 new CVEs:  CVE-2026-0002\n"}},
    ]
    records = {c["cveMetadata"]["cveId"]: c for c in (cve_a, cve_b)}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=records[cve_id])

    result = get_latest_cve_fact_sheet(client=_make_client(handler), commit_count=2)

    assert "CVE-2026-0002" in result
    assert "9.0" in result


def test_duplicate_cve_ids_deduplicated():
    """The same CVE ID appearing in multiple commits is fetched only once."""
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    commits = [
        {"commit": {"message": "1 new CVEs:  CVE-2026-0001\n"}},
        {"commit": {"message": "1 updated CVEs: CVE-2026-0001\n"}},
    ]

    request_count = {"cve_api": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        request_count["cve_api"] += 1
        return httpx.Response(200, json=cve)

    get_latest_cve_fact_sheet(client=_make_client(handler), commit_count=2)

    assert request_count["cve_api"] == 1


# --- max_records limit -----------------------------------------------------


def test_max_records_limits_fetches():
    cve_ids = [f"CVE-2026-{i:04d}" for i in range(1, 6)]
    commits = _commits_response(cve_ids)
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)

    request_count = {"cve_api": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        request_count["cve_api"] += 1
        return httpx.Response(200, json=cve)

    get_latest_cve_fact_sheet(client=_make_client(handler), max_records=2)

    assert request_count["cve_api"] == 2


# --- Empty / edge cases ----------------------------------------------------


def test_empty_commits_returns_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "No recent CVE IDs" in result


def test_commits_without_cve_ids_returns_message():
    commits = [{"commit": {"message": "some non-CVE commit message"}}]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=commits)

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "No recent CVE IDs" in result


def test_result_includes_data_source_attribution():
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=cve)

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "cveawg.mitre.org" in result
    assert "cvelistV5" in result


def test_result_includes_note_about_programmatic_selection():
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=cve)

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "programmatically" in result.lower()


# --- Fact-sheet truncation --------------------------------------------------


def test_cve_fact_sheet_capped_when_description_huge():
    cve = _make_cve(
        "CVE-2026-0001",
        "2026-08-28T15:23:00Z",
        score=9.8,
        description="D" * 5000,
    )
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=cve)

    result = get_latest_cve_fact_sheet(
        client=_make_client(handler), max_output_chars=600
    )

    assert len(result) <= 600
    assert "[truncated " in result
    assert "CVE_ID: CVE-2026-0001" in result
    assert "CVSS_SCORE: 9.8" in result
    assert "DATA_SOURCE" in result
    assert "Do not fabricate" in result


# --- Timeout resolution -----------------------------------------------------


def test_get_latest_cve_fact_sheet_reads_timeout_from_env(monkeypatch):
    """When ``EXEC_TIMEOUT`` is set, the internally-created client uses it."""
    monkeypatch.setenv("EXEC_TIMEOUT", "60")
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    original_client_init = httpx.Client.__init__

    def fake_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        captured["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(handler)
        original_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", fake_init)

    result = get_latest_cve_fact_sheet()

    assert captured["timeout"] == 60.0
    assert "No recent CVE IDs" in result


def test_cve_fact_sheet_short_unchanged():
    cve = _make_cve(
        "CVE-2026-0001",
        "2026-08-28T15:23:00Z",
        score=9.8,
        description="Buffer overflow in product X",
    )
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=cve)

    result = get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "[truncated" not in result
    assert "CVE_ID: CVE-2026-0001" in result
    assert "Buffer overflow in product X" in result
    assert "DATA_SOURCE: CVE.org" in result


# --- Unscored-window fallback -----------------------------------------------


def _serve(records: list[dict[str, Any]], commits: list[dict[str, Any]]):
    by_id = {c["cveMetadata"]["cveId"]: c for c in records}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        cve_id = str(req.url).rsplit("/", 1)[-1]
        return httpx.Response(200, json=by_id[cve_id])

    return handler


def test_unscored_latest_window_falls_back_with_note():
    batch = [
        _make_cve(f"CVE-2026-1{i:03d}", f"2026-11-15T21:1{i % 5}:00Z", score=None)
        for i in range(5)
    ]
    scored = _make_cve("CVE-2026-0001", "2026-11-15T20:40:00Z", score=8.8)
    records = [*batch, scored]
    commits = _commits_response([c["cveMetadata"]["cveId"] for c in records])

    result = get_latest_cve_fact_sheet(client=_make_client(_serve(records, commits)))

    assert result.startswith("CVE_ID: CVE-2026-0001")
    assert "CVSS_SCORE: 8.8" in result
    note = [line for line in result.splitlines() if line.startswith("FALLBACK_NOTE:")]
    assert len(note) == 1
    assert "newest 5 CVE(s)" in note[0]
    assert "2026-11-15T21:10:00Z" in note[0]
    assert "2026-11-15T21:14:00Z" in note[0]
    assert "no CVSS score yet" in note[0]


def test_scored_latest_window_has_no_fallback_note():
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    older = _make_cve("CVE-2026-0002", "2026-08-28T07:00:00Z", score=9.9)
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])

    result = get_latest_cve_fact_sheet(
        client=_make_client(_serve([cve, older], commits))
    )

    assert result.startswith("CVE_ID: CVE-2026-0001")
    assert "FALLBACK_NOTE" not in result


def test_no_cvss_anywhere_returns_message():
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None),
        _make_cve("CVE-2026-0002", "2026-08-28T07:00:00Z", score=None),
    ]
    commits = _commits_response(["CVE-2026-0001", "CVE-2026-0002"])

    result = get_latest_cve_fact_sheet(client=_make_client(_serve(records, commits)))

    assert "Retrieved 2 recent CVE records" in result
    assert "none had CVSS score data" in result


def test_default_discovery_reads_five_commits_and_fetches_up_to_fifty():
    cve_ids = [f"CVE-2026-{i:04d}" for i in range(1, 61)]
    commits = _commits_response(cve_ids)
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    seen: dict[str, Any] = {"commits_url": None, "cve_api": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            seen["commits_url"] = str(req.url)
            return httpx.Response(200, json=commits)
        seen["cve_api"] += 1
        return httpx.Response(200, json=cve)

    get_latest_cve_fact_sheet(client=_make_client(handler))

    assert "per_page=5" in seen["commits_url"]
    assert seen["cve_api"] == 50


def test_records_reported_in_discovery_order_despite_concurrency():
    """Concurrent fetching must not change which CVE wins: selection is a
    pure function of the records, and failed fetches are still skipped."""
    records = [
        _make_cve(f"CVE-2026-{i:04d}", "2026-08-28T15:23:00Z", score=float(i % 10))
        for i in range(1, 21)
    ]
    commits = _commits_response([c["cveMetadata"]["cveId"] for c in records])
    handler = _serve(records, commits)

    def flaky(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("CVE-2026-0009"):
            return httpx.Response(503)
        return handler(req)

    result = get_latest_cve_fact_sheet(client=_make_client(flaky))

    # 0009 (score 9.0) failed; 0019 (score 9.0) is the remaining top score.
    assert result.startswith("CVE_ID: CVE-2026-0019")
    assert "RECORDS_RETRIEVED: 19" in result


def test_fallback_note_survives_truncation_of_huge_description():
    unscored = _make_cve("CVE-2026-1000", "2026-11-15T21:10:00Z", score=None)
    scored = _make_cve(
        "CVE-2026-0001", "2026-11-15T20:40:00Z", score=8.8, description="D" * 5000
    )
    commits = _commits_response(["CVE-2026-1000", "CVE-2026-0001"])

    result = get_latest_cve_fact_sheet(
        client=_make_client(_serve([unscored, scored], commits)),
        max_output_chars=600,
    )

    assert "[truncated " in result
    assert (
        "FALLBACK_NOTE: the newest 1 CVE(s) (published 2026-11-15T21:10:00Z)" in result
    )


# --- Aggregate fetch deadline -------------------------------------------------


def _blocking_handler(
    records: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    slow_ids: set[str],
    release: threading.Event,
):
    serve = _serve(records, commits)

    def handler(req: httpx.Request) -> httpx.Response:
        cve_id = str(req.url).rsplit("/", 1)[-1]
        if cve_id in slow_ids:
            release.wait(10.0)
        return serve(req)

    return handler


def test_fetch_phase_stops_at_deadline():
    """Slow fetches are abandoned at the deadline (80 % of the timeout) and the
    selection uses the records that arrived in time."""
    records = [
        _make_cve(f"CVE-2026-{i:04d}", "2026-08-28T15:23:00Z", score=float(i))
        for i in range(1, 11)
    ]
    commits = _commits_response([c["cveMetadata"]["cveId"] for c in records])
    slow = {f"CVE-2026-{i:04d}" for i in range(6, 11)}  # the high scores
    release = threading.Event()
    client = _make_client(_blocking_handler(records, commits, slow, release))
    try:
        start = time.monotonic()
        result = get_latest_cve_fact_sheet(client=client, timeout=1.0)
        elapsed = time.monotonic() - start
    finally:
        release.set()

    assert elapsed < 1.5
    assert result.startswith("CVE_ID: CVE-2026-0005")
    assert "RECORDS_RETRIEVED: 5" in result


def test_deadline_with_no_records_returns_failure_message():
    records = [_make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=9.0)]
    commits = _commits_response(["CVE-2026-0001"])
    release = threading.Event()
    client = _make_client(
        _blocking_handler(records, commits, {"CVE-2026-0001"}, release)
    )
    try:
        start = time.monotonic()
        result = get_latest_cve_fact_sheet(client=client, timeout=0.5)
        elapsed = time.monotonic() - start
    finally:
        release.set()

    assert elapsed < 1.0
    assert result == "Failed to retrieve any CVE records from cveawg.mitre.org."

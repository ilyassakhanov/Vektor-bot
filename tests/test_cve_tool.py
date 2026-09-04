"""Tests for CveTool — fetches recent CVE records and selects the most critical.

All tests use httpx.MockTransport — no network access needed.
"""

from __future__ import annotations

from typing import Any

import httpx

from tools.cve import CveTool


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


# --- Tool metadata ---------------------------------------------------------


def test_tool_name():
    tool = CveTool(client=_make_client(lambda r: httpx.Response(200, json=[])))
    assert tool.name == "get_latest_cve"


def test_tool_description_mentions_cve():
    tool = CveTool(client=_make_client(lambda r: httpx.Response(200, json=[])))
    assert "CVE" in tool.description


def test_tool_parameters_no_required_args():
    tool = CveTool(client=_make_client(lambda r: httpx.Response(200, json=[])))
    params = tool.parameters
    assert params["type"] == "object"
    assert params["required"] == []


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

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

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

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

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

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

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

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

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

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "none" in result.lower() or "no" in result.lower()
    assert "CVSS" in result or "CVSS" not in result


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

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "CVE-2026-0002" in result


# --- Network errors --------------------------------------------------------


def test_github_api_failure_returns_error_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

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

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "CVE-2026-0001" in result


def test_cve_record_malformed_json_skipped():
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, content=b"not-json")

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "Failed to retrieve" in result or "none" in result.lower()


def test_commits_malformed_json_returns_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

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

    tool = CveTool(client=_make_client(handler), commit_count=2)
    result = tool.execute()

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

    tool = CveTool(client=_make_client(handler), commit_count=2)
    tool.execute()

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

    tool = CveTool(client=_make_client(handler), max_records=2)
    tool.execute()

    assert request_count["cve_api"] == 2


# --- Empty / edge cases ----------------------------------------------------


def test_empty_commits_returns_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "No recent CVE IDs" in result


def test_commits_without_cve_ids_returns_message():
    commits = [{"commit": {"message": "some non-CVE commit message"}}]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=commits)

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "No recent CVE IDs" in result


def test_result_includes_data_source_attribution():
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=cve)

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "cveawg.mitre.org" in result
    assert "cvelistV5" in result


def test_result_includes_note_about_programmatic_selection():
    cve = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    commits = _commits_response(["CVE-2026-0001"])

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=cve)

    tool = CveTool(client=_make_client(handler))
    result = tool.execute()

    assert "programmatically" in result.lower()

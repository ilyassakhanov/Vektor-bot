"""CVE core — fetches recent CVE records and selects the most critical one.

This module does all the heavy lifting programmatically so the LLM doesn't have
to: it discovers recently published CVE IDs from the official CVE Program
GitHub repository, retrieves each CVE record from the official CVE Services
API, and runs the deterministic :func:`select_cve` selector to pick the
single most critical CVE (highest CVSS in the latest publication window).

The LLM receives a compact, pre-formatted fact sheet and only needs to
write the final natural-language summary — no JSON parsing, score
comparison, or windowing logic on the LLM side.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from agent.cve_selector import select_cve
from tools.truncation import max_output_chars_from_env, truncate

log = logging.getLogger("vektor.cve_core")

_CVELIST_COMMITS_URL = (
    "https://api.github.com/repos/CVEProject/cvelistV5/commits?per_page={count}"
)
_CVE_RECORD_URL = "https://cveawg.mitre.org/api/cve/{cve_id}"
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d+")
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_COMMIT_COUNT = 2
_DEFAULT_MAX_RECORDS = 20


def _exec_timeout_from_env() -> float:
    """Resolve the exec timeout from ``EXEC_TIMEOUT``, falling back to default.

    Reads the ``EXEC_TIMEOUT`` environment variable. Unset or non-numeric
    values fall back to ``_DEFAULT_TIMEOUT`` (30.0). A valid numeric value
    is returned as ``float``.
    """
    raw = os.environ.get("EXEC_TIMEOUT")
    if raw is None:
        return _DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "Invalid EXEC_TIMEOUT %r, using default %.1f", raw, _DEFAULT_TIMEOUT
        )
        return _DEFAULT_TIMEOUT


def get_latest_cve_fact_sheet(
    client: httpx.Client | None = None,
    timeout: float | None = None,
    commit_count: int = _DEFAULT_COMMIT_COUNT,
    max_records: int = _DEFAULT_MAX_RECORDS,
    max_output_chars: int | None = None,
) -> str:
    """Retrieve the most critical recently-published CVE fact sheet.

    Discovers recent CVE IDs from the official CVE Program GitHub repo
    (``CVEProject/cvelistV5``), fetches each record from the official CVE
    Services API (``cveawg.mitre.org``), and deterministically selects the
    highest-scoring CVE from the latest publication window. Returns a
    compact fact sheet for the selected CVE (or an error message).

    All network access uses :mod:`httpx`. Failures are returned as strings
    — the function never raises for network/parse errors. The fact
    sheet is capped at ``max_output_chars`` (env ``EXEC_MAX_OUTPUT_CHARS``,
    default 4000) via the shared head+tail truncation helper.

    When ``timeout is None``, the timeout is resolved from the
    ``EXEC_TIMEOUT`` environment variable (default 30.0).
    """
    own_client = client is None
    resolved_timeout = _exec_timeout_from_env() if timeout is None else timeout
    http_client = client or httpx.Client(
        timeout=resolved_timeout,
        headers={"User-Agent": "vektor-bot"},
    )
    cap = max_output_chars_from_env(max_output_chars)
    try:
        log.info("cve_core: discovering recent CVE IDs")
        cve_ids = _discover_cve_ids(http_client, commit_count)
        if not cve_ids:
            return (
                "No recent CVE IDs found from the official CVE Program "
                "repository (CVEProject/cvelistV5). Try again later."
            )

        cve_ids = cve_ids[:max_records]
        log.info("cve_core: retrieving %d CVE records", len(cve_ids))

        records = _fetch_records(http_client, cve_ids)
        if not records:
            return "Failed to retrieve any CVE records from cveawg.mitre.org."

        selected = select_cve(records)
        if selected is None:
            return (
                f"Retrieved {len(records)} recent CVE records, but none had "
                "CVSS score data in the latest publication window."
            )

        log.info(
            "cve_core: selected %s (score=%s)",
            selected.cve_id,
            selected.cvss_score,
        )
        fact_sheet = _format_cve(selected, total_retrieved=len(records))
        return truncate(fact_sheet, cap)
    finally:
        if own_client:
            http_client.close()


def _discover_cve_ids(client: httpx.Client, commit_count: int) -> list[str]:
    """Fetch recent commits and extract unique CVE IDs."""
    url = _CVELIST_COMMITS_URL.format(count=commit_count)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Failed to fetch cvelistV5 commits: %s", exc)
        return []

    if not isinstance(data, list):
        return []

    seen: set[str] = set()
    ids: list[str] = []
    for commit in data:
        if not isinstance(commit, dict):
            continue
        msg_obj = commit.get("commit") or {}
        message = msg_obj.get("message") or ""
        for match in _CVE_ID_RE.findall(message):
            if match not in seen:
                seen.add(match)
                ids.append(match)
    log.info("Discovered %d unique CVE IDs", len(ids))
    return ids


def _fetch_records(client: httpx.Client, cve_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch CVE records for the given IDs from the CVE Services API."""
    records: list[dict[str, Any]] = []
    for cve_id in cve_ids:
        url = _CVE_RECORD_URL.format(cve_id=cve_id)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            records.append(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Failed to fetch CVE record %s: %s", cve_id, exc)
    return records


def _format_cve(cve: object, total_retrieved: int) -> str:
    """Format a CVEInfo into a compact fact sheet for the LLM."""
    cve_id = getattr(cve, "cve_id", "unknown")
    score = getattr(cve, "cvss_score", None)
    severity = getattr(cve, "cvss_severity", None)
    date_pub = getattr(cve, "date_published", None)
    vendor = getattr(cve, "vendor", None)
    product = getattr(cve, "product", None)
    description = getattr(cve, "description", None)
    attack_vector = getattr(cve, "attack_vector", None)

    lines = [
        f"CVE_ID: {cve_id}",
        f"CVSS_SCORE: {score if score is not None else 'N/A'}",
        f"CVSS_SEVERITY: {severity or 'N/A'}",
        f"PUBLISHED: {date_pub or 'N/A'}",
        f"VENDOR: {vendor or 'N/A'}",
        f"PRODUCT: {product or 'N/A'}",
        f"DESCRIPTION: {description or 'N/A'}",
        f"ATTACK_VECTOR: {attack_vector or 'N/A'}",
        f"RECORDS_RETRIEVED: {total_retrieved}",
        "DATA_SOURCE: CVE.org (cveawg.mitre.org/api/cve, CVEProject/cvelistV5)",
        (
            "NOTE: This CVE was selected programmatically (highest CVSS baseScore in "
            "the latest publication window). Do not fabricate any fields."
        ),
    ]
    return "\n".join(lines)

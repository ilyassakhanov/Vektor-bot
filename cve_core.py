"""CVE core — fetches recent CVE records and selects the most critical one.

This module does all the heavy lifting programmatically so the LLM doesn't have
to: it discovers recently published CVE IDs from the official CVE Program
GitHub repository, retrieves each CVE record from the official CVE Services
API, and runs the deterministic :func:`select_cve` selector to pick the
single most critical CVE (highest CVSS in the newest publication window
that has scored CVEs; newer all-unscored windows are skipped and reported
in a ``FALLBACK_NOTE:`` line).

The LLM receives a compact, pre-formatted fact sheet and only needs to
write the final natural-language summary — no JSON parsing, score
comparison, or windowing logic on the LLM side.
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

import httpx

from agent.cve_selector import CVESelection, select_cve_window
from tools.truncation import max_output_chars_from_env, truncate

log = logging.getLogger("vektor.cve_core")

_CVELIST_COMMITS_URL = (
    "https://api.github.com/repos/CVEProject/cvelistV5/commits?per_page={count}"
)
_CVE_RECORD_URL = "https://cveawg.mitre.org/api/cve/{cve_id}"
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d+")
_DEFAULT_TIMEOUT = 30.0
# Wide enough that an earlier scored window is reachable when the newest
# commits are one large unscored CNA batch.
_DEFAULT_COMMIT_COUNT = 5
_DEFAULT_MAX_RECORDS = 50
# Records are fetched concurrently; sequential fetches could take longer
# than the EXEC_TIMEOUT MCP round-trip even on a healthy API.
_FETCH_WORKERS = 8
# The whole run (discovery + fetches) must finish before the MCP round-trip
# (EXEC_TIMEOUT) is killed, so it stops at this fraction of the timeout and
# selects from the records fetched so far. A slow API otherwise needs several
# pool waves of per-request timeouts.
_DEADLINE_FRACTION = 0.8


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
    highest-scoring CVE from the newest publication window that has CVSS
    data (falling back past newer all-unscored windows). Returns a
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
    deadline = time.monotonic() + resolved_timeout * _DEADLINE_FRACTION
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

        records = _fetch_records(http_client, cve_ids, deadline)
        if not records:
            return "Failed to retrieve any CVE records from cveawg.mitre.org."

        selection = select_cve_window(records)
        if selection is None:
            return (
                f"Retrieved {len(records)} recent CVE records, but none had "
                "CVSS score data."
            )

        log.info(
            "cve_core: selected %s (score=%s, fallback=%s)",
            selection.cve.cve_id,
            selection.cve.cvss_score,
            selection.fallback,
        )
        fact_sheet = _format_cve(selection, total_retrieved=len(records))
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


def _fetch_records(
    client: httpx.Client, cve_ids: list[str], deadline: float
) -> list[dict[str, Any]]:
    """Fetch CVE records for the given IDs from the CVE Services API.

    Fetches run on a bounded thread pool (the ``httpx.Client`` is
    thread-safe); results keep discovery order and failed fetches are
    skipped. The phase stops at ``deadline`` (a ``time.monotonic()`` value):
    fetches not yet started are cancelled, fetches still in flight are
    abandoned, and only the records that arrived in time are returned.
    """

    def fetch(cve_id: str) -> dict[str, Any] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        url = _CVE_RECORD_URL.format(cve_id=cve_id)
        try:
            # Cap the request so an abandoned fetch ends near the deadline.
            resp = client.get(
                url, timeout=min(client.timeout.read or remaining, remaining)
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Failed to fetch CVE record %s: %s", cve_id, exc)
            return None
        except Exception as exc:  # noqa: BLE001 — e.g. client closed after the deadline
            log.debug("Abandoned CVE record fetch %s: %s", cve_id, type(exc).__name__)
            return None

    if not cve_ids:
        return []
    workers = min(_FETCH_WORKERS, len(cve_ids))
    # No ``with``: its exit would wait for fetches still in flight.
    pool = ThreadPoolExecutor(max_workers=workers)
    futures: list[Future[dict[str, Any] | None]] = [
        pool.submit(fetch, cve_id) for cve_id in cve_ids
    ]
    try:
        _, not_done = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not_done:
        log.warning(
            "cve_core: fetch deadline hit, %d/%d records pending",
            len(not_done),
            len(cve_ids),
        )
    records: list[dict[str, Any]] = []
    for future in futures:
        if future.done() and not future.cancelled():
            record = future.result()
            if record is not None:
                records.append(record)
    return records


def _format_cve(selection: CVESelection, total_retrieved: int) -> str:
    """Format a CVE selection into a compact fact sheet for the LLM."""
    cve = selection.cve
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
    ]
    # Placed before DESCRIPTION so head+tail truncation never drops it.
    if selection.fallback:
        lines.append(_fallback_note(selection))
    lines += [
        f"VENDOR: {vendor or 'N/A'}",
        f"PRODUCT: {product or 'N/A'}",
        f"DESCRIPTION: {description or 'N/A'}",
        f"ATTACK_VECTOR: {attack_vector or 'N/A'}",
        f"RECORDS_RETRIEVED: {total_retrieved}",
        "DATA_SOURCE: CVE.org (cveawg.mitre.org/api/cve, CVEProject/cvelistV5)",
        (
            "NOTE: This CVE was selected programmatically (highest CVSS baseScore in "
            "the newest publication window with scored CVEs). Do not fabricate "
            "any fields."
        ),
    ]
    return "\n".join(lines)


def _fallback_note(selection: CVESelection) -> str:
    """Describe the skipped unscored windows in one line."""
    newest, oldest = selection.skipped_newest, selection.skipped_oldest
    if newest and oldest and newest != oldest:
        when = f"published {oldest} to {newest}"
    elif newest or oldest:
        when = f"published {newest or oldest}"
    else:
        when = "publication time unknown"
    return (
        f"FALLBACK_NOTE: the newest {selection.skipped_unscored} CVE(s) "
        f"({when}) have no CVSS score yet; showing the most critical CVE "
        "from the latest scored window."
    )

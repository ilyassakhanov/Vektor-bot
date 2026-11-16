"""CVE core — fetches recent CVE records and selects the most critical one.

This module does all the heavy lifting programmatically so the LLM doesn't have
to: it discovers recently published CVE IDs from the official CVE Program
GitHub repository, retrieves each CVE record from the official CVE Services
API, and runs the deterministic :func:`select_cve_window` selector to pick the
single most critical CVE (highest CVSS in the newest publication window
that has scored CVEs; newer all-unscored windows are skipped and reported
in a ``FALLBACK_NOTE:`` line).

The LLM receives a compact, pre-formatted fact sheet and only needs to
write the final natural-language summary — no JSON parsing, score
comparison, or windowing logic on the LLM side.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

import httpx

from agent.cve_selector import CVESelection, select_cve_window
from config import exec_timeout_from_env
from tools.truncation import max_output_chars_from_env, truncate

log = logging.getLogger("vektor.cve_core")

_CVELIST_COMMITS_URL = (
    "https://api.github.com/repos/CVEProject/cvelistV5/commits?per_page={count}"
)
_CVE_RECORD_URL = "https://cveawg.mitre.org/api/cve/{cve_id}"
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d+")
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

_NO_IDS_MESSAGE = (
    "No recent CVE IDs found from the official CVE Program "
    "repository (CVEProject/cvelistV5). Try again later."
)
_DISCOVERY_TIMEOUT_MESSAGE = (
    "CVE lookup timed out before recent CVE IDs could be discovered from the "
    "official CVE Program repository (CVEProject/cvelistV5). Try again later."
)
_DISCOVERY_FAILED_MESSAGE = (
    "Failed to discover recent CVE IDs from the official CVE Program "
    "repository (CVEProject/cvelistV5). Try again later."
)


class _DiscoveryError(Exception):
    """Discovery could not run to completion; ``str(exc)`` is the user message."""


def _capped_timeout(
    client: httpx.Client,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> httpx.Timeout | None:
    """Return the client's timeout with every phase capped to the time left.

    Each httpx phase (connect/read/write/pool) becomes
    ``min(client phase, remaining)``; a phase the client leaves unlimited
    (``None``) becomes ``remaining``. A stricter client phase is kept.
    Returns None once ``deadline`` (a ``clock()`` value) has passed.
    """
    remaining = deadline - clock()
    if remaining <= 0:
        return None

    def cap(phase: float | None) -> float:
        return remaining if phase is None else min(phase, remaining)

    base = client.timeout
    return httpx.Timeout(
        connect=cap(base.connect),
        read=cap(base.read),
        write=cap(base.write),
        pool=cap(base.pool),
    )


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
    ``EXEC_TIMEOUT`` environment variable via :func:`config.positive_env`
    (default 30.0; invalid, ``<= 0``, NaN or inf → default with a warning;
    above ``config.MAX_TIMEOUT_S`` → clamped with a warning).
    """
    own_client = client is None
    resolved_timeout = exec_timeout_from_env() if timeout is None else timeout
    deadline = time.monotonic() + resolved_timeout * _DEADLINE_FRACTION
    http_client = client or httpx.Client(
        timeout=resolved_timeout,
        headers={"User-Agent": "vektor-bot"},
    )
    cap = max_output_chars_from_env(max_output_chars)
    try:
        log.info("cve_core: discovering recent CVE IDs")
        try:
            cve_ids = _discover_cve_ids(http_client, commit_count, deadline)
        except _DiscoveryError as exc:
            return str(exc)
        if not cve_ids:
            return _NO_IDS_MESSAGE

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


def _discover_cve_ids(
    client: httpx.Client, commit_count: int, deadline: float
) -> list[str]:
    """Fetch recent commits and extract unique CVE IDs.

    The request timeout is capped to the time left before ``deadline`` (a
    ``time.monotonic()`` value), so discovery shares the run's budget with
    the record fetches; past the deadline nothing is requested.

    An empty list means discovery succeeded but found no CVE IDs. A deadline
    hit or request timeout raises :class:`_DiscoveryError` with the timeout
    message; any other HTTP/parse failure raises it with the failure message.
    Malformed commit entries (non-dict entry, non-dict ``commit``, non-str
    ``message``) are skipped; a non-empty payload with no well-formed entry
    counts as a parse failure.
    """
    timeout = _capped_timeout(client, deadline)
    if timeout is None:
        log.warning("cve_core: deadline hit before CVE ID discovery")
        raise _DiscoveryError(_DISCOVERY_TIMEOUT_MESSAGE)
    url = _CVELIST_COMMITS_URL.format(count=commit_count)
    try:
        resp = client.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException as exc:
        log.warning("cvelistV5 commits request timed out: %s", type(exc).__name__)
        raise _DiscoveryError(_DISCOVERY_TIMEOUT_MESSAGE) from exc
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Failed to fetch cvelistV5 commits: %s", exc)
        raise _DiscoveryError(_DISCOVERY_FAILED_MESSAGE) from exc

    if not isinstance(data, list):
        log.warning("cvelistV5 commits response is not a list")
        raise _DiscoveryError(_DISCOVERY_FAILED_MESSAGE)

    seen: set[str] = set()
    ids: list[str] = []
    well_formed = 0
    for commit in data:
        message = _commit_message(commit)
        if message is None:
            continue
        well_formed += 1
        for match in _CVE_ID_RE.findall(message):
            if match not in seen:
                seen.add(match)
                ids.append(match)
    if data and not well_formed:
        log.warning("cvelistV5 commits response has no well-formed commit entries")
        raise _DiscoveryError(_DISCOVERY_FAILED_MESSAGE)
    if well_formed < len(data):
        log.warning(
            "Skipped %d malformed cvelistV5 commit entries", len(data) - well_formed
        )
    log.info("Discovered %d unique CVE IDs", len(ids))
    return ids


def _commit_message(commit: object) -> str | None:
    """Return a GitHub commit entry's message, or None if the entry is malformed.

    Well-formed means ``{"commit": {"message": str}}``; a missing/null
    ``commit`` or ``message`` counts as an empty message (no CVE IDs).
    """
    if not isinstance(commit, dict):
        return None
    msg_obj = commit.get("commit")
    if msg_obj is None:
        return ""
    if not isinstance(msg_obj, dict):
        return None
    message = msg_obj.get("message")
    if message is None:
        return ""
    if not isinstance(message, str):
        return None
    return message


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
        # Cap the request so an abandoned fetch ends near the deadline.
        timeout = _capped_timeout(client, deadline)
        if timeout is None:
            return None
        url = _CVE_RECORD_URL.format(cve_id=cve_id)
        try:
            resp = client.get(url, timeout=timeout)
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
    lines = [
        f"CVE_ID: {cve.cve_id}",
        f"CVSS_SCORE: {cve.cvss_score if cve.cvss_score is not None else 'N/A'}",
        f"CVSS_SEVERITY: {cve.cvss_severity or 'N/A'}",
        f"PUBLISHED: {cve.date_published or 'N/A'}",
    ]
    # Placed before DESCRIPTION so head+tail truncation never drops it.
    if selection.fallback:
        lines.append(_fallback_note(selection))
    lines += [
        f"VENDOR: {cve.vendor or 'N/A'}",
        f"PRODUCT: {cve.product or 'N/A'}",
        f"DESCRIPTION: {cve.description or 'N/A'}",
        f"ATTACK_VECTOR: {cve.attack_vector or 'N/A'}",
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
    if newest == oldest:
        when = f"published {newest}"
    else:
        when = f"published {oldest} to {newest}"
    return (
        f"FALLBACK_NOTE: the newest {selection.skipped_unscored} CVE(s) "
        f"({when}) have no CVSS score yet; showing the most critical CVE "
        "from the latest scored window."
    )

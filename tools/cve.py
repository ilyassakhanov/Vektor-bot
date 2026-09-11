"""CVE tool — fetches recent CVE records and selects the most critical one.

This tool does all the heavy lifting programmatically so the LLM doesn't have
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
import re
from typing import Any

import httpx

from agent.cve_selector import select_cve
from tools.base import Tool
from tools.truncation import max_output_chars_from_env, truncate

log = logging.getLogger("vektor.tools.cve")

_CVELIST_COMMITS_URL = (
    "https://api.github.com/repos/CVEProject/cvelistV5/commits?per_page={count}"
)
_CVE_RECORD_URL = "https://cveawg.mitre.org/api/cve/{cve_id}"
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d+")
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_COMMIT_COUNT = 2
_DEFAULT_MAX_RECORDS = 20


class CveTool(Tool):
    """Retrieve the most critical recently-published CVE.

    Discovers recent CVE IDs from the official CVE Program GitHub repo
    (``CVEProject/cvelistV5``), fetches each record from the official CVE
    Services API (``cveawg.mitre.org``), and deterministically selects the
    highest-scoring CVE from the latest publication window. Returns a
    compact fact sheet for the selected CVE (or an error message).

    All network access uses :mod:`httpx`. Failures are returned as strings
    to the LLM — the tool never raises for network/parse errors. The fact
    sheet is capped at ``max_output_chars`` (env ``EXEC_MAX_OUTPUT_CHARS``,
    default 4000) via the shared head+tail truncation helper.
    """

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        commit_count: int = _DEFAULT_COMMIT_COUNT,
        max_records: int = _DEFAULT_MAX_RECORDS,
        max_output_chars: int | None = None,
    ) -> None:
        self._timeout = timeout
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "vektor-bot"},
        )
        self._commit_count = commit_count
        self._max_records = max_records
        self._max_output_chars = max_output_chars_from_env(max_output_chars)

    @property
    def name(self) -> str:
        return "get_latest_cve"

    @property
    def description(self) -> str:
        return (
            "Get the most critical recent CVE (highest CVSS) from official "
            "CVE.org data. Use for latest or most severe CVE questions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def execute(self, **kwargs: Any) -> str:
        log.info("CveTool: discovering recent CVE IDs")
        cve_ids = self._discover_cve_ids()
        if not cve_ids:
            return (
                "No recent CVE IDs found from the official CVE Program "
                "repository (CVEProject/cvelistV5). Try again later."
            )

        cve_ids = cve_ids[: self._max_records]
        log.info("CveTool: retrieving %d CVE records", len(cve_ids))

        records = self._fetch_records(cve_ids)
        if not records:
            return "Failed to retrieve any CVE records from cveawg.mitre.org."

        selected = select_cve(records)
        if selected is None:
            return (
                f"Retrieved {len(records)} recent CVE records, but none had "
                "CVSS score data in the latest publication window."
            )

        log.info(
            "CveTool: selected %s (score=%s)", selected.cve_id, selected.cvss_score
        )
        fact_sheet = _format_cve(selected, total_retrieved=len(records))
        return truncate(fact_sheet, self._max_output_chars)

    def _discover_cve_ids(self) -> list[str]:
        """Fetch recent commits and extract unique CVE IDs."""
        url = _CVELIST_COMMITS_URL.format(count=self._commit_count)
        try:
            resp = self._client.get(url)
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

    def _fetch_records(self, cve_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch CVE records for the given IDs from the CVE Services API."""
        records: list[dict[str, Any]] = []
        for cve_id in cve_ids:
            url = _CVE_RECORD_URL.format(cve_id=cve_id)
            try:
                resp = self._client.get(url)
                resp.raise_for_status()
                records.append(resp.json())
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Failed to fetch CVE record %s: %s", cve_id, exc)
        return records

    def close(self) -> None:
        self._client.close()


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

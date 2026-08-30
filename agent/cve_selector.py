"""CVE selection — deterministic latest-window + highest-score algorithm.

This is a pure function: takes raw CVE record dicts (as returned by the
CVE Services API at cveawg.mitre.org/api/cve/:id), extracts the relevant
fields, and selects the correct CVE per the task rules.

Algorithm:
1. Extract CVEInfo from each raw record.
2. Filter out CVEs without CVSS data (they cannot win).
3. Find the latest publication timestamp among the remaining CVEs.
4. Latest window = CVEs published within 5 minutes of that timestamp.
5. Within the window, select the highest CVSS baseScore.
6. If scores tie, select the most recently published.
7. Return the selected CVEInfo, or None if no CVE with CVSS exists.

CVEs without CVSS are excluded before windowing — they don't participate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("vektor.agent.cve_selector")

_WINDOW_MINUTES = 5.0


@dataclass(frozen=True)
class CVEInfo:
    """Extracted CVE information — a normalised view of a raw CVE record."""

    cve_id: str
    date_published: str
    cvss_score: float | None
    cvss_severity: str | None
    description: str | None
    vendor: str | None
    product: str | None
    attack_vector: str | None


def select_cve(records: list[dict[str, Any]]) -> CVEInfo | None:
    """Select the highest-scoring CVE from the latest publication window.

    Returns None if no CVE with CVSS data exists in the latest window.
    """
    if not records:
        return None

    infos = [_extract_cve_info(r) for r in records]
    if not infos:
        return None

    # Determine the latest publication timestamp from ALL records (including
    # those without CVSS — they define the window but cannot win).
    latest_pub = max(infos, key=lambda c: c.date_published).date_published
    log.info("Latest publication: %s", latest_pub)

    # Filter to the latest window — all CVEs published within the window.
    window = _filter_latest_window(infos, latest_pub)
    log.info("CVEs in latest window: %d", len(window))

    # Within the window, only CVEs with CVSS can participate in the comparison.
    with_scores = [c for c in window if c.cvss_score is not None]
    if not with_scores:
        log.info("No CVEs with CVSS data in the latest window")
        return None

    winner = max(
        with_scores,
        key=lambda c: (c.cvss_score or 0.0, c.date_published),
    )
    log.info(
        "Selected: %s (score=%s, published=%s)",
        winner.cve_id,
        winner.cvss_score,
        winner.date_published,
    )
    return winner


def _filter_latest_window(cves: list[CVEInfo], latest_ts: str) -> list[CVEInfo]:
    """Return CVEs published within _WINDOW_MINUTES of the latest timestamp."""
    from datetime import datetime, timedelta

    try:
        latest_dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return cves

    cutoff = latest_dt - timedelta(minutes=_WINDOW_MINUTES)
    result: list[CVEInfo] = []
    for cve in cves:
        try:
            cve_dt = datetime.fromisoformat(cve.date_published.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            result.append(cve)
            continue
        if cve_dt >= cutoff:
            result.append(cve)
    return result


def _extract_cve_info(record: dict[str, Any]) -> CVEInfo:
    """Extract normalised CVEInfo from a raw CVE record dict."""
    meta = record.get("cveMetadata") or {}
    cve_id = str(meta.get("cveId") or "")
    date_published = str(meta.get("datePublished") or meta.get("dateUpdated") or "")

    containers = record.get("containers") or {}
    cna = containers.get("cna") or {}
    adp = containers.get("adp") or []

    description = _extract_description(cna)
    vendor, product = _extract_affected(cna)
    score, severity, vector = _extract_cvss(cna, adp)

    return CVEInfo(
        cve_id=cve_id,
        date_published=date_published,
        cvss_score=score,
        cvss_severity=severity,
        description=description,
        vendor=vendor,
        product=product,
        attack_vector=vector,
    )


def _extract_description(cna: dict[str, Any]) -> str | None:
    descriptions = cna.get("descriptions") or []
    for d in descriptions:
        if isinstance(d, dict) and d.get("lang") == "en":
            return str(d.get("value") or "")
    if descriptions and isinstance(descriptions[0], dict):
        return str(descriptions[0].get("value") or "")
    return None


def _extract_affected(cna: dict[str, Any]) -> tuple[str | None, str | None]:
    affected = cna.get("affected") or []
    if affected and isinstance(affected[0], dict):
        a = affected[0]
        return (
            str(a.get("vendor") or "") or None,
            str(a.get("product") or "") or None,
        )
    return None, None


def _extract_cvss(
    cna: dict[str, Any], adp: list[Any]
) -> tuple[float | None, str | None, str | None]:
    """Extract CVSS score/severity/vector, preferring CNA over ADP."""
    score, severity, vector = _extract_cvss_from_metrics(cna.get("metrics") or [])
    if score is not None:
        return score, severity, vector

    for adp_container in adp:
        if isinstance(adp_container, dict):
            s, sev, vec = _extract_cvss_from_metrics(adp_container.get("metrics") or [])
            if s is not None:
                return s, sev, vec
    return None, None, None


def _extract_cvss_from_metrics(
    metrics: list[Any],
) -> tuple[float | None, str | None, str | None]:
    """Extract CVSS from a metrics list, checking cvssV3_1, cvssV3_0, cvssV4_0."""
    for m in metrics:
        if not isinstance(m, dict):
            continue
        for key in ("cvssV3_1", "cvssV3_0", "cvssV4_0", "cvssV2_0"):
            cvss = m.get(key)
            if isinstance(cvss, dict):
                score = cvss.get("baseScore")
                if score is not None:
                    return (
                        float(score),
                        str(cvss.get("baseSeverity") or "") or None,
                        str(cvss.get("vectorString") or "") or None,
                    )
    return None, None, None

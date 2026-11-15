"""CVE selection — deterministic windowed + highest-score algorithm.

This is a pure function: takes raw CVE record dicts (as returned by the
CVE Services API at cveawg.mitre.org/api/cve/:id), extracts the relevant
fields, and selects the correct CVE per the task rules.

Algorithm:
1. Extract CVEInfo from each raw record.
2. Group ALL records into 5-minute publication windows, walking backwards
   from the newest timestamp: window 1 is [newest - 5 min, newest]; each next
   window is anchored at the newest timestamp strictly older than the
   previous window's cutoff.
3. Scan windows newest first and use the first one containing a CVE with
   CVSS data (CVEs without CVSS define windows but cannot win).
4. Within that window, select the highest CVSS baseScore.
5. If scores tie, select the most recently published.
6. Return None only if no record at all has CVSS data.

When newer windows were skipped because they were unscored, the selection is
a *fallback*; :func:`select_cve_window` reports how many records were skipped
and their publication range so callers can say so transparently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


@dataclass(frozen=True)
class CVESelection:
    """The selected CVE plus fallback details.

    ``fallback`` is True when one or more newer windows held only unscored
    CVEs and were skipped. ``skipped_unscored`` counts the records in those
    windows; ``skipped_newest``/``skipped_oldest`` are their publication
    range (None when nothing was skipped).
    """

    cve: CVEInfo
    fallback: bool = False
    skipped_unscored: int = 0
    skipped_newest: str | None = None
    skipped_oldest: str | None = None


def select_cve(records: list[dict[str, Any]]) -> CVEInfo | None:
    """Select the highest-scoring CVE from the newest scored window.

    Returns None if no record has CVSS data. See :func:`select_cve_window`
    for the fallback details.
    """
    selection = select_cve_window(records)
    return selection.cve if selection is not None else None


def select_cve_window(records: list[dict[str, Any]]) -> CVESelection | None:
    """Select the highest-scoring CVE from the newest window with CVSS data.

    Returns None if no record has CVSS data.
    """
    if not records:
        return None

    infos = [_extract_cve_info(r) for r in records]
    windows = _group_windows(infos)
    log.info("CVE windows: %d (records=%d)", len(windows), len(infos))

    skipped: list[CVEInfo] = []
    for window in windows:
        with_scores = [c for c in window if c.cvss_score is not None]
        if not with_scores:
            skipped.extend(window)
            continue
        winner = max(
            with_scores,
            key=lambda c: (c.cvss_score or 0.0, c.date_published),
        )
        log.info(
            "Selected: %s (score=%s, published=%s, skipped_unscored=%d)",
            winner.cve_id,
            winner.cvss_score,
            winner.date_published,
            len(skipped),
        )
        if not skipped:
            return CVESelection(cve=winner)
        stamps = [c.date_published for c in skipped if _parse_ts(c.date_published)]
        stamps.sort(key=lambda ts: _parse_ts(ts) or datetime.min.replace(tzinfo=UTC))
        return CVESelection(
            cve=winner,
            fallback=True,
            skipped_unscored=len(skipped),
            skipped_newest=stamps[-1] if stamps else None,
            skipped_oldest=stamps[0] if stamps else None,
        )

    log.info("No CVEs with CVSS data in any window")
    return None


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _group_windows(cves: list[CVEInfo]) -> list[list[CVEInfo]]:
    """Group CVEs into deterministic windows, newest window first.

    Records with an unparseable timestamp join the first (newest) window, as
    in the original single-window behaviour. If no timestamp parses, all
    records form one window.
    """
    dated: list[tuple[datetime, CVEInfo]] = []
    undated: list[CVEInfo] = []
    for cve in cves:
        dt = _parse_ts(cve.date_published)
        if dt is None:
            undated.append(cve)
        else:
            dated.append((dt, cve))

    if not dated:
        return [undated]

    dated.sort(key=lambda pair: pair[0], reverse=True)
    span = timedelta(minutes=_WINDOW_MINUTES)
    windows: list[list[CVEInfo]] = []
    current: list[CVEInfo] = []
    cutoff: datetime | None = None
    for dt, cve in dated:
        if cutoff is None or dt < cutoff:
            if current:
                windows.append(current)
            current = []
            cutoff = dt - span
        current.append(cve)
    windows.append(current)
    windows[0].extend(undated)
    return windows


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

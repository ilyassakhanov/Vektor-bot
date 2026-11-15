"""Tests for CVESelector — deterministic latest-window + highest-score selection.

The selector takes raw CVE record dicts (as returned by cveawg.mitre.org/api/cve/:id)
and selects the correct one. It is a pure function: no network, no side effects.

Selection algorithm:
1. Group records into 5-minute windows walking backwards from the newest
   publication timestamp (each next window is anchored at the newest
   timestamp strictly older than the previous window's cutoff).
2. Use the newest window that holds at least one CVE with CVSS.
3. Within that window, select the highest CVSS baseScore.
4. If scores tie, select the most recently published.
5. CVEs without CVSS data cannot win; None only if no record has CVSS.
"""

from __future__ import annotations

from agent.cve_selector import CVESelection, select_cve, select_cve_window


def _make_cve(
    cve_id: str,
    date_published: str,
    score: float | None = None,
    severity: str | None = None,
    description: str = "A vulnerability",
    vendor: str = "vendor",
    product: str = "product",
    vector: str = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
) -> dict:
    """Build a minimal CVE record dict matching the CVE Services API format."""
    metrics: list[dict] = []
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


# --- Latest-window selection ------------------------------------------------


def test_selects_from_latest_publication_window():
    """A high-score CVE from an older window must NOT win over a lower-score
    CVE from the latest window."""
    older_high = _make_cve("CVE-2026-0001", "2026-08-28T07:30:00Z", score=9.9)
    newer_low = _make_cve("CVE-2026-0002", "2026-08-28T15:23:00Z", score=5.0)
    result = select_cve([older_high, newer_low])
    assert result is not None
    assert result.cve_id == "CVE-2026-0002"


def test_same_window_selects_highest_score():
    cve_a = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.5)
    cve_b = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=9.8)
    cve_c = _make_cve("CVE-2026-0003", "2026-08-28T15:23:45Z", score=6.0)
    result = select_cve([cve_a, cve_b, cve_c])
    assert result is not None
    assert result.cve_id == "CVE-2026-0002"
    assert result.cvss_score == 9.8


# --- Tie-breaking by publication time ---------------------------------------


def test_equal_scores_tiebreak_by_most_recent():
    cve_a = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=9.8)
    cve_b = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=9.8)
    result = select_cve([cve_a, cve_b])
    assert result is not None
    assert result.cve_id == "CVE-2026-0002"


def test_equal_scores_tiebreak_by_most_recent_reversed_order():
    """Order in the list must not matter."""
    cve_a = _make_cve("CVE-2026-0001", "2026-08-28T15:23:30Z", score=9.8)
    cve_b = _make_cve("CVE-2026-0002", "2026-08-28T15:23:00Z", score=9.8)
    result = select_cve([cve_b, cve_a])
    assert result is not None
    assert result.cve_id == "CVE-2026-0001"


# --- Missing CVSS -----------------------------------------------------------


def test_missing_cvss_cannot_win():
    cve_no_score = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None)
    cve_with_score = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=5.0)
    result = select_cve([cve_no_score, cve_with_score])
    assert result is not None
    assert result.cve_id == "CVE-2026-0002"
    assert result.cvss_score == 5.0


def test_all_missing_cvss_returns_none():
    cve_a = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None)
    cve_b = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=None)
    result = select_cve([cve_a, cve_b])
    assert result is None


def test_unscored_latest_window_falls_back_to_older_scored_window():
    """An unscored latest window no longer blocks selection: the newest
    earlier window with a scored CVE supplies the winner."""
    cve_no_score_new = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None)
    cve_with_score_old = _make_cve("CVE-2026-0002", "2026-08-28T07:30:00Z", score=9.0)
    result = select_cve([cve_no_score_new, cve_with_score_old])
    assert result is not None
    assert result.cve_id == "CVE-2026-0002"


# --- First API result not automatically selected ---------------------------


def test_first_result_not_automatically_selected():
    """Even if the first record has a high score, it must not win if a later
    record in the same window has a higher score."""
    first = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.0)
    second = _make_cve("CVE-2026-0002", "2026-08-28T15:23:30Z", score=8.0)
    result = select_cve([first, second])
    assert result is not None
    assert result.cve_id == "CVE-2026-0002"


def test_first_result_with_highest_score_in_older_window_loses():
    first = _make_cve("CVE-2026-0001", "2026-08-28T07:00:00Z", score=9.9)
    second = _make_cve("CVE-2026-0002", "2026-08-28T15:23:00Z", score=3.0)
    result = select_cve([first, second])
    assert result is not None
    assert result.cve_id == "CVE-2026-0002"


# --- CVE ID not used as recency proxy --------------------------------------


def test_cve_id_order_not_used_as_recency_proxy():
    """A CVE with a numerically higher ID but older publication date must
    NOT be selected over a CVE with a lower ID but newer publication date."""
    low_id_new = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=5.0)
    high_id_old = _make_cve("CVE-2026-9999", "2026-08-28T07:00:00Z", score=9.0)
    result = select_cve([high_id_old, low_id_new])
    assert result is not None
    assert result.cve_id == "CVE-2026-0001"


# --- CVEInfo extraction -----------------------------------------------------


def test_cve_info_has_all_fields():
    record = _make_cve(
        "CVE-2026-0001",
        "2026-08-28T15:23:00Z",
        score=9.8,
        severity="CRITICAL",
        description="Buffer overflow in product X",
        vendor="acme",
        product="widget",
        vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    )
    result = select_cve([record])
    assert result is not None
    assert result.cve_id == "CVE-2026-0001"
    assert result.cvss_score == 9.8
    assert result.cvss_severity == "CRITICAL"
    assert result.date_published == "2026-08-28T15:23:00Z"
    assert result.vendor == "acme"
    assert result.product == "widget"
    assert "Buffer overflow" in (result.description or "")
    assert result.attack_vector is not None


def test_cve_info_attack_vector_from_cvss_vector():
    record = _make_cve(
        "CVE-2026-0001",
        "2026-08-28T15:23:00Z",
        score=7.5,
        vector="CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:H/I:N/A:N",
    )
    result = select_cve([record])
    assert result is not None
    assert "AV:L" in (result.attack_vector or "")


def test_cve_info_with_no_description():
    record = _make_cve(
        "CVE-2026-0001", "2026-08-28T15:23:00Z", score=5.0, description=""
    )
    result = select_cve([record])
    assert result is not None
    assert result.cve_id == "CVE-2026-0001"


# --- Edge cases -------------------------------------------------------------


def test_empty_list_returns_none():
    assert select_cve([]) is None


def test_single_record_with_score():
    record = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=7.5)
    result = select_cve([record])
    assert result is not None
    assert result.cve_id == "CVE-2026-0001"
    assert result.cvss_score == 7.5


def test_single_record_without_score_returns_none():
    record = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None)
    result = select_cve([record])
    assert result is None


def test_records_with_adp_cvss():
    """CVSS may come from ADP containers when the CNA didn't provide one."""
    record = {
        "cveMetadata": {
            "cveId": "CVE-2026-0001",
            "state": "PUBLISHED",
            "datePublished": "2026-08-28T15:23:00Z",
            "dateUpdated": "2026-08-28T15:23:00Z",
        },
        "containers": {
            "cna": {
                "descriptions": [{"lang": "en", "value": "A vuln"}],
                "affected": [{"vendor": "v", "product": "p"}],
                "metrics": [],
            },
            "adp": [
                {
                    "metrics": [
                        {
                            "cvssV3_1": {
                                "baseScore": 9.1,
                                "baseSeverity": "CRITICAL",
                                "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                            }
                        }
                    ],
                }
            ],
        },
    }
    result = select_cve([record])
    assert result is not None
    assert result.cve_id == "CVE-2026-0001"
    assert result.cvss_score == 9.1


def test_prefers_cna_cvss_over_adp():
    """When both CNA and ADP provide CVSS, CNA's score is used."""
    record = {
        "cveMetadata": {
            "cveId": "CVE-2026-0001",
            "state": "PUBLISHED",
            "datePublished": "2026-08-28T15:23:00Z",
            "dateUpdated": "2026-08-28T15:23:00Z",
        },
        "containers": {
            "cna": {
                "descriptions": [{"lang": "en", "value": "A vuln"}],
                "affected": [{"vendor": "v", "product": "p"}],
                "metrics": [
                    {
                        "cvssV3_1": {
                            "baseScore": 7.5,
                            "baseSeverity": "HIGH",
                            "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
                        }
                    }
                ],
            },
            "adp": [
                {
                    "metrics": [
                        {
                            "cvssV3_1": {
                                "baseScore": 9.8,
                                "baseSeverity": "CRITICAL",
                                "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                            }
                        }
                    ]
                }
            ],
        },
    }
    result = select_cve([record])
    assert result is not None
    assert result.cvss_score == 7.5


def test_records_with_cvss_v4():
    record = {
        "cveMetadata": {
            "cveId": "CVE-2026-0001",
            "state": "PUBLISHED",
            "datePublished": "2026-08-28T15:23:00Z",
            "dateUpdated": "2026-08-28T15:23:00Z",
        },
        "containers": {
            "cna": {
                "descriptions": [{"lang": "en", "value": "A vuln"}],
                "affected": [{"vendor": "v", "product": "p"}],
                "metrics": [
                    {
                        "cvssV4_0": {
                            "baseScore": 8.2,
                            "baseSeverity": "HIGH",
                            "vectorString": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H",
                        }
                    }
                ],
            },
        },
    }
    result = select_cve([record])
    assert result is not None
    assert result.cvss_score == 8.2


# --- Multiple windows with many records -------------------------------------


def test_complex_scenario_multiple_windows_and_scores():
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T07:00:00Z", score=9.9),
        _make_cve("CVE-2026-0002", "2026-08-28T07:00:30Z", score=8.0),
        _make_cve("CVE-2026-0003", "2026-08-28T11:00:00Z", score=7.5),
        _make_cve("CVE-2026-0004", "2026-08-28T15:23:00Z", score=6.0),
        _make_cve("CVE-2026-0005", "2026-08-28T15:23:15Z", score=9.0),
        _make_cve("CVE-2026-0006", "2026-08-28T15:23:30Z", score=9.0),
        _make_cve("CVE-2026-0007", "2026-08-28T15:23:45Z", score=None),
    ]
    result = select_cve(records)
    assert result is not None
    # Latest window = 15:23:xx. Highest score = 9.0. Tie between CVE-0005 and CVE-0006.
    # Tiebreak = most recent → CVE-0006 (15:23:30 > 15:23:15)
    assert result.cve_id == "CVE-2026-0006"
    assert result.cvss_score == 9.0


# --- Unscored-window fallback -----------------------------------------------


def test_bulk_unscored_batch_falls_back_with_metadata():
    """A bulk unscored batch (like a CNA dump) is skipped; the most critical
    CVE of the newest scored window is chosen and the fallback is reported."""
    batch = [
        _make_cve(f"CVE-2026-1{i:03d}", f"2026-11-15T21:1{i % 5}:00Z", score=None)
        for i in range(20)
    ]
    older = [
        _make_cve("CVE-2026-0001", "2026-11-15T20:40:00Z", score=6.5),
        _make_cve("CVE-2026-0002", "2026-11-15T20:42:00Z", score=8.8),
        _make_cve("CVE-2026-0003", "2026-11-15T19:00:00Z", score=10.0),
    ]
    selection = select_cve_window(batch + older)
    assert isinstance(selection, CVESelection)
    assert selection.cve.cve_id == "CVE-2026-0002"
    assert selection.fallback is True
    assert selection.skipped_unscored == 20
    assert selection.skipped_newest == "2026-11-15T21:14:00Z"
    assert selection.skipped_oldest == "2026-11-15T21:10:00Z"


def test_multiple_unscored_windows_skipped():
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None),
        _make_cve("CVE-2026-0002", "2026-08-28T15:22:00Z", score=None),
        _make_cve("CVE-2026-0003", "2026-08-28T15:10:00Z", score=None),
        _make_cve("CVE-2026-0004", "2026-08-28T15:00:00Z", score=4.0),
        _make_cve("CVE-2026-0005", "2026-08-28T14:57:00Z", score=7.0),
        _make_cve("CVE-2026-0006", "2026-08-28T14:00:00Z", score=9.9),
    ]
    selection = select_cve_window(records)
    assert selection is not None
    # Window 1 = 15:18–15:23 (0001, 0002), window 2 = 15:05–15:10 (0003),
    # window 3 = 14:55–15:00 (0004, 0005) → 0005 wins with 7.0.
    assert selection.cve.cve_id == "CVE-2026-0005"
    assert selection.fallback is True
    assert selection.skipped_unscored == 3
    assert selection.skipped_newest == "2026-08-28T15:23:00Z"
    assert selection.skipped_oldest == "2026-08-28T15:10:00Z"


def test_windows_anchor_on_newest_timestamp_older_than_cutoff():
    """Windows are anchored deterministically: the second window starts at
    the newest timestamp strictly older than the first window's cutoff, not
    at a fixed 5-minute grid."""
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None),
        # 15:17:59 is just older than the 15:18:00 cutoff → anchors window 2,
        # which spans 15:12:59–15:17:59.
        _make_cve("CVE-2026-0002", "2026-08-28T15:17:59Z", score=None),
        _make_cve("CVE-2026-0003", "2026-08-28T15:13:30Z", score=5.0),
        _make_cve("CVE-2026-0004", "2026-08-28T15:12:00Z", score=9.0),
    ]
    selection = select_cve_window(records)
    assert selection is not None
    # Window 2 holds 0002 (unscored) and 0003 (5.0); 0004 at 15:12:00 falls
    # outside it, so its higher 9.0 score does not compete.
    assert selection.cve.cve_id == "CVE-2026-0003"
    assert selection.fallback is True
    assert selection.skipped_unscored == 1


def test_no_cvss_anywhere_returns_none_across_windows():
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None),
        _make_cve("CVE-2026-0002", "2026-08-28T10:00:00Z", score=None),
        _make_cve("CVE-2026-0003", "2026-08-27T10:00:00Z", score=None),
    ]
    assert select_cve_window(records) is None
    assert select_cve(records) is None


def test_scored_latest_window_is_not_a_fallback():
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T07:00:00Z", score=9.9),
        _make_cve("CVE-2026-0002", "2026-08-28T15:23:00Z", score=3.0),
        _make_cve("CVE-2026-0003", "2026-08-28T15:23:30Z", score=None),
    ]
    selection = select_cve_window(records)
    assert selection is not None
    assert selection.cve.cve_id == "CVE-2026-0002"
    assert selection.fallback is False
    assert selection.skipped_unscored == 0
    assert selection.skipped_newest is None
    assert selection.skipped_oldest is None


def test_fallback_tie_picks_most_recent_in_window():
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None),
        _make_cve("CVE-2026-0002", "2026-08-28T12:00:00Z", score=8.0),
        _make_cve("CVE-2026-0003", "2026-08-28T12:01:00Z", score=8.0),
    ]
    result = select_cve(records)
    assert result is not None
    assert result.cve_id == "CVE-2026-0003"


def test_fallback_ignores_record_order():
    scored = _make_cve("CVE-2026-0002", "2026-08-28T12:00:00Z", score=6.0)
    older_higher = _make_cve("CVE-2026-0003", "2026-08-28T09:00:00Z", score=9.8)
    unscored = _make_cve("CVE-2026-0001", "2026-08-28T15:23:00Z", score=None)
    for order in (
        [older_higher, scored, unscored],
        [unscored, older_higher, scored],
        [scored, unscored, older_higher],
    ):
        result = select_cve(order)
        assert result is not None
        assert result.cve_id == "CVE-2026-0002"


def test_mixed_timezone_formats_are_comparable():
    """Naive timestamps are treated as UTC and compare with Z/offset ones."""
    records = [
        _make_cve("CVE-2026-0001", "2026-08-28T15:23:00.000", score=None),
        _make_cve("CVE-2026-0002", "2026-08-28T15:00:00+00:00", score=7.0),
    ]
    selection = select_cve_window(records)
    assert selection is not None
    assert selection.cve.cve_id == "CVE-2026-0002"
    assert selection.fallback is True

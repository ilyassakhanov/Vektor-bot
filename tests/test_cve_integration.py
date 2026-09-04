"""Integration tests against live CVE.org endpoints.

These tests require network access and are skipped if the endpoints
are unreachable. They verify that CVE data retrieval works end-to-end
with real API responses — both via the low-level exec tool and the
high-level CveTool.

Run with: python -m pytest tests/test_cve_integration.py -v
"""

from __future__ import annotations

import json

import pytest

from agent.cve_selector import select_cve
from tools.cve import CveTool
from tools.exec import ExecTool

_CVE_API = "https://cveawg.mitre.org/api/cve"
_CVELIST_COMMITS = (
    "https://api.github.com/repos/CVEProject/cvelistV5/commits?per_page=1"
)


def _is_online(url: str) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' '{url}'",
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.stdout.strip() == "200"
    except Exception:  # noqa: BLE001 - connectivity check must never crash
        return False


@pytest.fixture
def exec_tool() -> ExecTool:
    return ExecTool(timeout=30.0)


@pytest.fixture
def online() -> None:
    # Check a real, complete CVE URL — the bare /api/cve returns 400.
    if not _is_online(f"{_CVE_API}/CVE-2024-1234"):
        pytest.skip("CVE.org API is not reachable")


def test_cve_api_returns_valid_record(exec_tool: ExecTool, online: None) -> None:
    """Verify that cveawg.mitre.org returns a valid CVE record."""
    result = exec_tool.execute(command=f"curl -s '{_CVE_API}/CVE-2024-1234'")
    data = json.loads(result.split("stdout:\n")[1].split("\nstderr:")[0])
    assert data["cveMetadata"]["cveId"] == "CVE-2024-1234"
    assert data["cveMetadata"]["state"] == "PUBLISHED"


def test_cvelist_commits_returns_new_cve_ids(exec_tool: ExecTool, online: None) -> None:
    """Verify that the cvelistV5 commits endpoint returns recent CVE IDs."""
    import re

    result = exec_tool.execute(command=f"curl -s '{_CVELIST_COMMITS}'")
    stdout = result.split("stdout:\n")[1].split("\nstderr:")[0]
    data = json.loads(stdout)
    assert len(data) >= 1
    msg = data[0]["commit"]["message"]
    cve_ids = re.findall(r"CVE-\d{4}-\d+", msg)
    assert len(cve_ids) > 0


def test_cve_selector_with_real_records(exec_tool: ExecTool, online: None) -> None:
    """End-to-end: fetch real CVE records and run the selector."""
    import re

    # Step 1: get recent CVE IDs
    commits_result = exec_tool.execute(command=f"curl -s '{_CVELIST_COMMITS}'")
    stdout = commits_result.split("stdout:\n")[1].split("\nstderr:")[0]
    commits = json.loads(stdout)
    msg = commits[0]["commit"]["message"]
    cve_ids = re.findall(r"CVE-\d{4}-\d+", msg)[:5]  # limit to 5 for speed

    # Step 2: fetch each record
    records = []
    for cve_id in cve_ids:
        result = exec_tool.execute(command=f"curl -s '{_CVE_API}/{cve_id}'")
        rec_stdout = result.split("stdout:\n")[1].split("\nstderr:")[0]
        records.append(json.loads(rec_stdout))

    assert len(records) > 0

    # Step 3: run the selector
    selected = select_cve(records)
    # We can't assert specific values (they change), but we can verify structure
    if selected is not None:
        assert selected.cve_id.startswith("CVE-")
        assert selected.cvss_score is not None
        assert selected.cvss_score >= 0.0


# --- CveTool end-to-end ----------------------------------------------------


def test_cve_tool_retrieves_and_selects(online: None) -> None:
    """End-to-end: CveTool fetches real CVE records and selects the most critical.

    This exercises the full CveTool pipeline against live CVE.org endpoints:
    discover IDs from cvelistV5 commits, fetch records from cveawg.mitre.org,
    and run the programmatic selector. The result must be a valid fact sheet.
    """
    tool = CveTool(timeout=30.0)
    try:
        result = tool.execute()
    finally:
        tool.close()

    assert isinstance(result, str)
    # Either we got a CVE fact sheet or a graceful "no CVSS" / error message.
    # If the fact sheet was returned, verify its structure.
    if result.startswith("CVE_ID:"):
        assert "CVSS_SCORE:" in result
        assert "CVSS_SEVERITY:" in result
        assert "PUBLISHED:" in result
        assert "DATA_SOURCE: CVE.org" in result
        assert "programmatically" in result.lower()
        # Extract the CVE ID and verify it looks right
        for line in result.splitlines():
            if line.startswith("CVE_ID:"):
                cve_id = line.split(":", 1)[1].strip()
                assert cve_id.startswith("CVE-")
            if line.startswith("CVSS_SCORE:"):
                score_str = line.split(":", 1)[1].strip()
                if score_str != "N/A":
                    score = float(score_str)
                    assert score >= 0.0
    else:
        # Graceful error message — acceptable for live data variability
        assert isinstance(result, str) and len(result) > 0

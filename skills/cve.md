# Skill: CVE — Latest High-Scoring CVE Summary

## Objective

When a user asks about the latest CVEs, the highest-scoring CVE, or a CVE
summary, use the `get_latest_cve` tool and summarize its output.

## Workflow

1. Call the `get_latest_cve` tool (no arguments needed).
2. The tool does everything programmatically:
   - Discovers recently published CVE IDs from the official CVE Program
     repository (CVEProject/cvelistV5).
   - Retrieves each CVE record from the official CVE Services API
     (cveawg.mitre.org/api/cve).
   - Selects the single most critical CVE: highest CVSS baseScore within
     the latest publication window (5 minutes). Ties broken by most recent
     publication time. CVEs without CVSS cannot win.
3. The tool returns a compact fact sheet with these fields:
   - `CVE_ID`
   - `CVSS_SCORE` and `CVSS_SEVERITY`
   - `PUBLISHED` (publication timestamp)
   - `VENDOR` and `PRODUCT`
   - `DESCRIPTION`
   - `ATTACK_VECTOR`
   - `RECORDS_RETRIEVED`
   - `DATA_SOURCE`
4. Write a concise summary using ONLY the fields in the fact sheet.

## Response Format

Include, when available:

- CVE ID
- CVSS score and severity (e.g., "9.8 CRITICAL")
- Publication date
- Affected vendor and product
- Vulnerability description
- Attack vector and impact
- Why it matters — a brief, factual explanation of the risk

## Rules

- Do NOT call `exec` or `curl` manually — the tool handles all data retrieval.
- Do NOT invent or fabricate any fields not present in the fact sheet.
- If the tool returns an error or "no CVSS" message, relay that to the user.
- Clearly identify the official source: CVE.org (cveawg.mitre.org/api/cve).
- The CVE was selected programmatically by highest CVSS baseScore — trust it.

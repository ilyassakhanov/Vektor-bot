For the latest or highest-scoring CVE, call the `get_latest_cve` tool (no
arguments), then write a concise summary of its fact sheet (CVE ID, CVSS
score and severity, publication date, vendor and product, description,
attack vector).

Rules:

- Use ONLY fields from the fact sheet — never fabricate CVE facts.
- All facts come from CVE.org; never fetch CVE data via exec or curl.
- If the fact sheet has a `FALLBACK_NOTE:` line, tell the user that the newest CVEs have no CVSS score yet and that this CVE comes from the latest scored window.
- If the tool errors or reports no CVSS, relay that message to the user.

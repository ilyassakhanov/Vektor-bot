# Delta: exec-tool

## MODIFIED Requirements

### Requirement: Invalid EXEC_TIMEOUT falls back

`EXEC_TIMEOUT` SHALL be read through one shared parser by the exec tool, the
MCP round-trip and the CVE lookup. A value that is non-numeric, `<= 0`, NaN,
infinite or too large for a float SHALL fall back to 30 seconds with a
warning; a value above 3600 SHALL be clamped to 3600 with a warning. An
explicit MCP `call_timeout` that is not a positive finite number SHALL raise
`ValueError`.

#### Scenario: Invalid timeout falls back

- **WHEN** `EXEC_TIMEOUT` is `abc`, `nan`, `inf`, `0` or `-5`
- **THEN** startup succeeds, a warning is logged, and every call site uses 30 seconds

#### Scenario: Huge timeout is clamped

- **WHEN** `EXEC_TIMEOUT` is `1e15`
- **THEN** startup succeeds, a warning is logged, and every call site uses 3600 seconds

#### Scenario: Explicit zero call timeout is rejected

- **WHEN** `McpStdioClient` is built with `call_timeout=0.0`
- **THEN** it raises `ValueError` instead of silently using the env value

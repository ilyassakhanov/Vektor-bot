# Delta: exec-tool

## ADDED Requirements

### Requirement: Invalid EXEC_TIMEOUT falls back

`EXEC_TIMEOUT` SHALL be read through one shared parser by the exec tool, the
MCP round-trip and the CVE lookup. A value that is non-numeric, `<= 0`, NaN or
infinite SHALL fall back to the default 30 seconds with a warning instead of
crashing startup or producing an unusable deadline.

#### Scenario: Invalid timeout falls back

- **WHEN** `EXEC_TIMEOUT` is `abc`, `nan`, `inf`, `0` or `-5`
- **THEN** startup succeeds, a warning is logged, and every call site uses 30 seconds

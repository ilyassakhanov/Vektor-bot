# Delta: agent-loop

## ADDED Requirements

### Requirement: Invalid iteration limit falls back

A non-integer or < 1 `AGENT_MAX_ITERATIONS` SHALL fall back to the default 8
with a warning, instead of crashing startup or producing an agent that never
calls the LLM.

#### Scenario: Invalid iteration limit falls back

- **WHEN** `AGENT_MAX_ITERATIONS` is `eight`, `0` or negative
- **THEN** the default 8 is used, a warning is logged, and the agent still calls the LLM

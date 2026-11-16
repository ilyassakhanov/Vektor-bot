# Delta: agent-loop

## ADDED Requirements

### Requirement: Agent rejects a non-positive iteration limit

Constructing an `Agent` with `max_iterations` below 1 SHALL raise `ValueError`,
so no agent exists that would never call the LLM. The `AGENT_MAX_ITERATIONS`
env path keeps its warn-and-fallback to 8.

#### Scenario: Zero iterations is rejected

- **WHEN** an `Agent` is constructed with `max_iterations` of `0` or a negative number
- **THEN** a `ValueError` is raised

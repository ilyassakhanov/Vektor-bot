# Delta: agent-loop

## ADDED Requirements

### Requirement: Wall-clock budget per run

The agent SHALL stop a run once its elapsed wall-clock time reaches
`AGENT_MAX_RUN_SECONDS` (default 120; a value of 0 or less disables the
budget), checked before every iteration after the first and before every tool
call. When it trips, remaining tool calls SHALL get a skipped result, and the
agent SHALL append and return a budget notice without another LLM call and
increment `vektor_agent_budget_exceeded_total`.

#### Scenario: The budget trips between iterations

- **WHEN** the LLM keeps requesting tools and the elapsed time reaches the budget before the next iteration
- **THEN** the agent returns the budget notice and makes no further LLM call

#### Scenario: The budget trips between tool calls of one reply

- **WHEN** one LLM reply requests several tool calls and the budget is reached after some of them
- **THEN** the remaining calls are not executed, each gets a `skipped: run budget exceeded` result, and the agent returns the budget notice without another LLM call

#### Scenario: A non-positive budget disables the check

- **WHEN** `AGENT_MAX_RUN_SECONDS` is `0` or negative
- **THEN** the run is bounded only by the iteration cap

#### Scenario: The first iteration always runs

- **WHEN** a run starts
- **THEN** the first LLM call is made regardless of the budget

### Requirement: Tool calls per reply are capped

The agent SHALL execute at most `AGENT_MAX_TOOL_CALLS_PER_TURN` (default 4)
tool calls from one LLM reply. Every extra call SHALL get a
`skipped: too many tool calls` result, so each call id is answered.

#### Scenario: Extra tool calls are skipped

- **WHEN** one LLM reply requests six tool calls and the cap is 2
- **THEN** two calls are executed, four get the skipped result, and the next LLM call sees six tool results

### Requirement: Base prompt forbids host-modifying commands

The base system prompt SHALL instruct the model never to run commands that stop,
signal or modify processes, files, services or the host, even if the user insists
or confirms. Shell lookups are limited to network read-only actions. This is defense
in depth only. The enforcement boundary is the exec tool.

#### Scenario: The host-safety section is present

- **WHEN** the system prompt is built
- **THEN** it contains the host-safety section after the existing base prompt sections

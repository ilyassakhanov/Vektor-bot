# Delta: agent-loop

## ADDED Requirements

### Requirement: Bounded agent loop

The agent SHALL answer a user message by repeatedly calling the LLM with the
conversation history, tool definitions and skills. Each LLM response either
requests tool executions — which the agent performs and feeds back before the
next LLM call — or is a final answer that ends the run. The loop SHALL stop
after at most `AGENT_MAX_ITERATIONS` LLM calls (default 8); when the limit is
reached without a final answer, the agent SHALL return a max-iterations
notice instead of calling the LLM again.

#### Scenario: Final answer ends the run

- **WHEN** the LLM returns a final answer on the third call
- **THEN** the agent returns that answer and performs no further LLM calls

#### Scenario: Iteration cap stops the loop

- **WHEN** the LLM keeps requesting tools for `AGENT_MAX_ITERATIONS` calls in a row
- **THEN** the agent stops and returns the max-iterations notice, and the loop never runs unbounded

#### Scenario: Tool results feed the next call

- **WHEN** the LLM requests a tool execution
- **THEN** the agent executes it, appends the result to the message history, and the next LLM call sees it

### Requirement: Tools are invoked only through the registry

The agent SHALL invoke tools exclusively through the `ToolRegistry`. A tool
name the registry does not know SHALL produce a tool-error result that is fed
back to the LLM as that tool's outcome, not an exception. Adding a tool
SHALL require only implementing the `Tool` interface and registering it —
the loop itself does not change.

#### Scenario: Unknown tool does not crash the run

- **WHEN** the LLM requests a tool that is not registered
- **THEN** the agent continues the run with an error result for that call, and the run still ends in a final answer or the iteration cap

#### Scenario: A new tool joins without loop changes

- **WHEN** a new `Tool` implementation is registered in the composition root
- **THEN** the agent can execute it on the next run without any change to the loop code

### Requirement: Provider-agnostic LLM boundary

The agent SHALL depend only on the `LLM` interface from `llm/base.py`. The
concrete provider is selected in the `bot.py` composition root; all Ollama
request and response handling SHALL stay confined to `llm/ollama.py`. LLM
failures SHALL surface as `LLMError`; the message handler catches them and
sends the user a friendly failure message while polling continues.

#### Scenario: Provider swap touches only the composition root

- **WHEN** a second `LLM` implementation replaces `OllamaLLM` in `build_llm()`
- **THEN** the agent, handler and conversation manager run unchanged

#### Scenario: LLM failure produces one friendly reply

- **WHEN** the LLM raises `LLMError` during a run
- **THEN** the user receives a single friendly error message and the bot keeps processing later messages

### Requirement: Skills are injected as instructions

Skill files (`skills/*.md`, discovered by `SkillLoader`) SHALL be injected
into the LLM system prompt as instructions. Adding a skill SHALL NOT require
a code change; non-markdown files in the directory are ignored.

#### Scenario: A dropped markdown file becomes a skill

- **WHEN** a new `.md` file is placed in `skills/`
- **THEN** its instructions appear in the system prompt of the next agent run


--------Second prompt(Agentic Version)-----------------------
<task>
Extend the existing Telegram bot with a minimal autonomous AI agent.

The project already has:

* Telegram bot;
* provider-independent LLM interface;
* `llm/ollama.py` using `httpx`.

Preserve the existing architecture and working functionality. Do not bypass or rewrite the LLM abstraction. </task>

<goal>
The agent's primary task is:

"Find the latest published CVEs, select the highest-scoring CVE among that latest publication window, and summarize it for the Telegram user."

CVE facts MUST come from official CVE.org data, not the LLM's knowledge or third-party databases. </goal>

<architecture>
Use this dependency flow:

```
Telegram → Agent/Harness → LLM interface → OllamaLLM
```

The Agent depends only on the abstract LLM interface.

Tools are separate:

```
Agent → ToolRegistry → ExecTool
```

Skills are separate:

```
Agent → SkillLoader → *.md
```

Do not put provider, tool, CVE, or Skill-specific logic into Telegram handlers or the Agent loop. </architecture>

<agent_loop>
Implement a bounded agentic loop:

1. Receive a user message.
2. Add it to the chat context.
3. Call the LLM with conversation, tools, and Skills.
4. If the LLM requests a tool, execute it and add the result to context.
5. Repeat until the LLM returns a final answer.
6. Return the final answer to Telegram.

Default maximum: `8` iterations, configurable.

Never allow an infinite loop.
</agent_loop>

<conversation>
Each Telegram chat is one continuous conversation.

Maintain:

```
chat_id → conversation context
```

Context must include user messages, assistant responses, tool calls, and tool results.

Different chats MUST be isolated.

Implement `/new`:

* clear only the current chat's context;
* create a new context;
* do not send `/new` to the LLM;
* confirm the new conversation.

In-memory storage is sufficient; persistence is not required. </conversation>

<exec>
Implement a generic `exec` tool using the system shell.

It must:

* accept a command;
* execute it;
* return stdout, stderr, and exit code;
* enforce a configurable timeout;
* return failures to the LLM instead of crashing the bot.

Its primary purpose is executing read-only HTTP requests such as:

```
curl <official CVE.org endpoint>
```

Do not hardcode CVE-specific behavior into `exec`. </exec>

<tools>
Create a minimal Tool abstraction and ToolRegistry.

The Agent must invoke tools through the registry rather than knowing individual tool implementations.

Adding another tool must not require changing the agentic loop. </tools>

<cve>
Use official CVE.org data.

Before implementation, inspect the current official CVE.org documentation and determine the correct supported mechanism for retrieving published CVE records. Do NOT invent an API endpoint.

Prefer an official public API when suitable. If CVE Services is not intended for public retrieval, use the official CVE.org-supported CVE List/data mechanism instead.

Do NOT use:

* NVD;
* third-party vulnerability databases;
* search engines;
* LLM knowledge as the source of current CVE facts.

The implementation must obtain, when available:

* CVE ID;
* publication timestamp;
* CVSS score/severity;
* description;
* affected product/vendor;
* attack/impact information.

  </cve>

<cve_selection>
Interpret "latest CVE with the highest score" deterministically:

1. Retrieve a set of recently published CVEs.
2. Determine publication timestamps from the returned records.
3. Identify the latest publication window represented by those records.
4. Compare CVSS scores within that latest window.
5. Select the highest available CVSS score.
6. If scores tie, select the most recently published CVE.
7. CVEs without CVSS data cannot win the comparison.

Never:

* select the first API result;
* use CVE ID ordering as a proxy for recency;
* select the globally highest score regardless of publication date;
* invent missing data.
  </cve_selection>

<cve_skill>
Create:

```
skills/cve.md
```

and a simple Skill loader that discovers `.md` files without requiring Python changes.

The Skill must instruct the agent how to:

* use the official CVE.org data source;
* retrieve recent records;
* use `curl` through `exec`;
* identify publication timestamps;
* extract CVSS scores;
* compare records;
* handle missing scores;
* apply the latest-window and tie-breaking rules;
* produce the final CVE summary.

The Skill contains instructions, not executable code.

Do not hardcode these instructions in Python.
</cve_skill>

<cve_response>
The final response should concisely include, when available:

* CVE ID;
* CVSS score/severity;
* publication date;
* affected vendor/product;
* vulnerability description;
* attack vector/impact;
* why it matters.

Do not fabricate missing information. Clearly distinguish API facts from interpretation and identify the official source.
</cve_response>

<llm>
Preserve the existing LLM abstraction.

`ollama.py` remains the first provider and must continue using `httpx`, not an Ollama SDK.

The Agent MUST NOT import or instantiate `OllamaLLM` directly.

The architecture must allow:

```
OllamaLLM
OtherLLM
```

to be substituted without modifying Agent or Telegram code.

If the current LLM interface cannot represent conversation history, tool calls, and tool results, extend it cleanly. </llm>

<skills_and_tools_contract>
The LLM must receive enough structured information to understand:

* available tools;
* tool parameters;
* tool results;
* available Skill instructions.

Do not rely on parsing arbitrary natural-language text to determine tool calls if the existing LLM integration can support structured tool calls.
</skills_and_tools_contract>

<testing>
Use TDD.

Tests must not require Telegram, Ollama, or live CVE.org access.

Mock external boundaries.

Test at minimum:

* normal Agent request;
* tool call → execution → result → next LLM call;
* multiple iterations;
* maximum iteration protection;
* tool failure;
* CVE records with different publication dates/scores;
* latest-window selection;
* highest-score selection;
* equal-score tie-breaking;
* missing CVSS;
* first API result not automatically selected;
* CVE ID not used as a recency proxy;
* Skill discovery/loading;
* conversation persistence within a chat;
* chat isolation;
* `/new`;
* Agent works with a fake LLM implementation and has no Ollama dependency.

  </testing>

<quality>
Use:
- Python type hints;
- static type checking;
- existing formatter/linter;
- dependency injection;
- focused classes;
- minimal global state;
- existing project conventions.

Avoid unnecessary abstractions and refactoring. </quality>

<process>
Before coding:

1. Inspect the existing project.
2. Understand Telegram, LLM, configuration, dependencies, and tests.
3. Inspect current official CVE.org documentation.
4. Determine the correct official retrieval mechanism.
5. Write tests first.
6. Implement incrementally.
7. Run tests, formatter, linter, and type checker.
8. Fix all failures.
9. If network access is available, perform one read-only integration test against CVE.org.

Do not modify unrelated functionality. </process>

<structure>
Adapt the existing project. A reasonable structure is:

```
llm/
  base.py
  ollama.py

agent/
  agent.py
  harness.py

tools/
  base.py
  registry.py
  exec.py

skills/
  loader.py
  cve.md

tests/
```

Do not create unnecessary files. </structure>

<acceptance>
The implementation is complete when:

* Telegram messages go through the Agent.
* Per-chat continuous context works.
* `/new` resets only the current chat.
* Agentic loop supports multiple tool calls and has an 8-step default limit.
* `exec` executes commands with timeout and returns stdout/stderr/exit code.
* Skills are loaded dynamically from `.md` files.
* `cve.md` provides the CVE workflow.
* Official CVE.org data is used.
* The Agent identifies the latest publication window.
* It selects the highest CVSS score within that window.
* Ties use publication time.
* Missing CVSS is handled correctly.
* Final output summarizes the selected CVE without fabricated facts.
* Agent depends only on the LLM abstraction.
* Ollama remains replaceable.
* Tests, linting, and type checking pass.

  </acceptance>

<final_output>
After implementation, report briefly:

1. Files changed.
2. Architecture implemented.
3. CVE retrieval mechanism and why it was chosen.
4. CVE selection algorithm.
5. Skill/tool implementation.
6. Configuration/run instructions.
7. Test, lint, and type-check results.
   </final_output>
-------Second prompt end--------

--------Fix prompt-----------
Agent must become leaner. qwen3.5:9b isn't hadling it well and isn't producing meaningful results. Agent must focus on 1 most critical CVE. That most critical CVE must be picked based on basescore programmatically and not use llm to do That
--------End prompt-----------
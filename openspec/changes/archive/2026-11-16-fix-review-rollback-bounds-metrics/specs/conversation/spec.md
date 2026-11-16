# Delta: conversation

## MODIFIED Requirements

### Requirement: Context is capped with the latest message kept

After every agent run, successful or failed, the stored history SHALL be
capped at `CONVERSATION_MAX_MESSAGES` messages (default 12): the oldest
messages are trimmed, and the latest user message SHALL always survive the
trim. Invalid environment values fall back to the default with a warning
instead of crashing.

#### Scenario: Oldest messages are trimmed first

- **WHEN** a chat exceeds the cap after an agent run
- **THEN** the history keeps the newest `CONVERSATION_MAX_MESSAGES` messages and the latest user message is present

#### Scenario: Invalid cap falls back

- **WHEN** `CONVERSATION_MAX_MESSAGES` is set to a non-integer
- **THEN** the default 12 is used and a warning is logged

## ADDED Requirements

### Requirement: Failed runs roll the history back

When an agent run raises, the chat history SHALL be restored to its exact
pre-turn state: the user message and any assistant or tool messages the run
appended are removed, and the error propagates to the caller. Repeated
failures SHALL NOT accumulate user messages.

#### Scenario: Consecutive LLM errors leave history unchanged

- **WHEN** several messages in a row fail with an LLM error
- **THEN** the history equals its state before the first failure, and the next successful run sees no orphan user messages

#### Scenario: Partial tool messages are removed

- **WHEN** a run fails after a tool call and its result were appended
- **THEN** the user message, the tool-call message and the tool result are all removed

### Requirement: One public turn entry point

`ConversationManager.handle(chat_id, text, *, reservation=None)` SHALL be the
only public entry point for a turn. Without a reservation it takes the chat's
busy mark; with one it runs inside that held mark. The document-upload turn
SHALL go through it, so an override of `handle` sees every turn. A reservation
no longer held or for another chat SHALL be rejected.

#### Scenario: An overridden handle sees the upload turn

- **WHEN** a subclass overrides `handle` and a document is uploaded
- **THEN** the override is called for the upload turn with the held reservation

#### Scenario: A stale reservation is rejected

- **WHEN** `handle` is called with a reservation whose block has exited
- **THEN** it raises and no agent run happens

# conversation Specification

## Purpose
Defines per-chat conversation memory: isolation between chats, the context
cap that trims the oldest messages while always keeping the latest user
message, and the `/new` reset.

## Requirements

### Requirement: One conversation per chat

Each Telegram chat SHALL be one continuous conversation. The conversation
manager keeps `chat_id -> messages` in memory; messages from one chat SHALL
NOT appear in another chat's history or influence its answers.

#### Scenario: Chats are isolated

- **WHEN** two chats exchange messages interleaved
- **THEN** each agent run receives only its own chat's history

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

### Requirement: /new resets only the current chat

The `/new` command SHALL clear only the issuing chat's context. It SHALL NOT
call the LLM, send anything to it, or affect other chats.

#### Scenario: Reset is local and silent to the LLM

- **WHEN** a user sends `/new` in a chat with prior history
- **THEN** that chat's next run starts from an empty context, no LLM call is made for `/new` itself, and other chats keep their history

### Requirement: Busy chats are rejected without waiting

Agent runs for one chat SHALL NOT overlap and a caller SHALL NOT wait. A message for a chat with a run or reservation in flight, `/new` included, SHALL get exactly one chat-busy reply distinct from the per-user busy reply, with no LLM call and the history untouched. A reservation SHALL hold the chat for its whole block and release it on any exit. Different chats SHALL run in parallel.

#### Scenario: A second message in a busy chat is rejected at once

- **WHEN** a message arrives in a chat while an agent run for that chat is in progress
- **THEN** the sender gets exactly one chat-busy reply without waiting, no LLM call is made, and the history is untouched

#### Scenario: Chat-busy reply differs from the per-user busy reply

- **WHEN** a member of a group chat writes while another member's run in that chat is in progress
- **THEN** the reply says the bot is answering another message in this chat, not the sender's previous message

#### Scenario: /new in a busy chat does not clear

- **WHEN** `/new` arrives in a chat while an agent run for that chat is in progress
- **THEN** a chat-busy reply is sent immediately and the history is not cleared

#### Scenario: Upload turn in a busy chat

- **WHEN** a document is uploaded in a chat while an agent run for that chat is in progress
- **THEN** the document is not downloaded or ingested and exactly one reply says it was not ingested and should be sent again

#### Scenario: A reservation holds the chat across its block

- **WHEN** a chat is reserved and a message for that chat arrives before the block ends
- **THEN** the message gets a chat-busy reply, while messages in other chats are answered

#### Scenario: Other chats are not blocked

- **WHEN** a run in one chat is in progress
- **THEN** a message in another chat is answered without waiting for it

#### Scenario: A failing run releases the chat

- **WHEN** an agent run or a reserved block raises
- **THEN** the next message in that chat is processed normally

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

# Delta: conversation

## ADDED Requirements

### Requirement: One conversation per chat

Each Telegram chat SHALL be one continuous conversation. The conversation
manager keeps `chat_id -> messages` in memory; messages from one chat SHALL
NOT appear in another chat's history or influence its answers.

#### Scenario: Chats are isolated

- **WHEN** two chats exchange messages interleaved
- **THEN** each agent run receives only its own chat's history

### Requirement: Context is capped with the latest message kept

After every agent run the stored history SHALL be capped at
`CONVERSATION_MAX_MESSAGES` messages (default 12): the oldest messages are
trimmed, and the latest user message SHALL always survive the trim. Invalid
environment values fall back to the default with a warning instead of
crashing.

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

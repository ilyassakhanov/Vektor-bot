# Delta: conversation

## REMOVED Requirements

### Requirement: Runs are serialized per chat

**Reason**: The blocking per-chat lock parked TeleBot workers behind a running
chat, so one busy group chat could stall every other chat.

**Migration**: Replaced by "Busy chats are rejected without waiting".

## ADDED Requirements

### Requirement: Busy chats are rejected without waiting

Agent runs for one chat SHALL NOT overlap, and a caller SHALL NOT wait for
another run. A message for a chat with a run in flight, `/new` and the
document-upload turn included, SHALL be rejected immediately with exactly one
busy reply, no LLM call and the history untouched. Different chats SHALL run
in parallel, the busy mark SHALL be cleared when a run raises, and only chats
with a run in flight are tracked.

#### Scenario: A second message in a busy chat is rejected at once

- **WHEN** a message arrives in a chat while an agent run for that chat is in progress
- **THEN** the sender gets exactly one busy reply without waiting, no LLM call is made, and the history is untouched

#### Scenario: /new in a busy chat does not clear

- **WHEN** `/new` arrives in a chat while an agent run for that chat is in progress
- **THEN** a busy reply is sent immediately and the history is not cleared

#### Scenario: Upload turn in a busy chat

- **WHEN** a document is uploaded in a chat while an agent run for that chat is in progress
- **THEN** the document is still ingested and exactly one reply says it is ingested and the bot is busy

#### Scenario: Other chats are not blocked

- **WHEN** a run in one chat is in progress
- **THEN** a message in another chat is answered without waiting for it

#### Scenario: A failing run releases the chat

- **WHEN** an agent run raises
- **THEN** the next message in that chat is processed normally

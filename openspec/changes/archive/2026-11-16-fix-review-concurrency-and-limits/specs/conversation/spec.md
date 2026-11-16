# Delta: conversation

## ADDED Requirements

### Requirement: Runs are serialized per chat

Agent runs for one chat SHALL NOT overlap. A per-chat lock SHALL be held for
the whole agent run plus the history trim, and `/new` and the document-upload
turn SHALL take the same lock, so concurrent messages from several users in
one group chat are processed one after another on a consistent history. Runs
in different chats SHALL NOT block each other, no global lock is held across
runs, and the lock SHALL be released when a run raises.

#### Scenario: Two concurrent messages in one chat do not interleave

- **WHEN** a second message arrives in a chat while an agent run for that chat is executing a tool
- **THEN** the second run starts only after the first finished, and the history holds the first run's messages followed by the second's

#### Scenario: /new waits for a running run

- **WHEN** `/new` arrives in a chat while an agent run for that chat is in progress
- **THEN** the reset happens after the run finished, never mid-run

#### Scenario: Other chats are not blocked

- **WHEN** a run in one chat is in progress
- **THEN** a message in another chat is answered without waiting for it

#### Scenario: A failing run releases the chat

- **WHEN** an agent run raises
- **THEN** the next message in that chat is processed normally

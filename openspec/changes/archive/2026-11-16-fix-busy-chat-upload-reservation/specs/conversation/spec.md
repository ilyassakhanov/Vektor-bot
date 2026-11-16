# Delta: conversation

## MODIFIED Requirements

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

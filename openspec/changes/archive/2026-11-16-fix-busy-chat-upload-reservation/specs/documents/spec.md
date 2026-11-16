# Delta: documents

## ADDED Requirements

### Requirement: Uploads reserve the chat before ingestion

A document upload SHALL reserve its chat after the auth gate and rate limiter and before any download, and SHALL hold the reservation across extraction, ingestion and the agent turn, releasing it on every exit path. An upload into a busy chat SHALL get exactly one reply saying the document was not ingested and should be resent, with no download, no ingestion and no LLM call.

#### Scenario: Upload into a busy chat is not ingested

- **WHEN** a document is uploaded in a chat while another run for that chat is in progress
- **THEN** nothing is downloaded or ingested, no LLM call is made, and exactly one reply says the document was not ingested and should be sent again

#### Scenario: Message during an upload

- **WHEN** a text message arrives in a chat while that chat's upload is being ingested
- **THEN** it gets a chat-busy reply, and the upload finishes with its staged replies and the agent's reply

#### Scenario: Failed upload releases the chat

- **WHEN** download, extraction, ingestion or the upload's agent turn fails
- **THEN** exactly one friendly error reply is sent and the chat accepts the next message

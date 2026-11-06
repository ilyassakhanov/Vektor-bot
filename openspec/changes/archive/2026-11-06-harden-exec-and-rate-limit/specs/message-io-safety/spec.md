# Delta: message-io-safety

## ADDED Requirements

### Requirement: Per-user rate limiting

After the auth gate and before the document branch, the handler SHALL admit
each user to at most one in-flight message and at most
`RATE_LIMIT_MAX_MESSAGES` (default 10) per sliding `RATE_LIMIT_WINDOW_S`
seconds (default 60). A rejected message SHALL get exactly one friendly reply,
SHALL NOT reach the LLM or the history, and SHALL be counted in
`vektor_rate_limited_total{reason}`. Uploads, `/new` and messages later
rejected by input validation SHALL each use a slot.

#### Scenario: A concurrent second message gets a busy reply

- **WHEN** a user sends a message while their previous message is still being handled
- **THEN** they receive exactly one busy reply, no LLM call is made, and the conversation history is untouched

#### Scenario: The window limit rejects excess messages

- **WHEN** a user has had `RATE_LIMIT_MAX_MESSAGES` messages accepted within the window and sends another
- **THEN** they receive exactly one rate-limited reply and no LLM call is made

#### Scenario: Uploads are counted

- **WHEN** a window-exhausted user uploads a document
- **THEN** the upload is rejected before the file is downloaded

#### Scenario: New-conversation command is counted

- **WHEN** a user sends `/new`
- **THEN** it consumes a window slot like any other message

#### Scenario: Validation rejects still use a slot

- **WHEN** an admitted message is then rejected as empty or too long
- **THEN** it has still used one window slot

#### Scenario: Users are independent

- **WHEN** one user is rate limited
- **THEN** another user's messages are still admitted

#### Scenario: The slot is released after an agent error

- **WHEN** the agent raises an LLM error while handling a message
- **THEN** the user's next message is admitted

### Requirement: Configurable worker threads

The TeleBot worker thread count SHALL come from `BOT_WORKER_THREADS` (default 4,
at most 64).

#### Scenario: Worker threads are configurable

- **WHEN** `BOT_WORKER_THREADS` is unset, invalid or below 1
- **THEN** the bot uses 4 worker threads and logs a warning for an invalid value

#### Scenario: Worker threads are clamped

- **WHEN** `BOT_WORKER_THREADS` is above 64
- **THEN** the bot uses 64 worker threads and logs a warning

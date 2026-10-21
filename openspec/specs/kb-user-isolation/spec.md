# kb-user-isolation Specification

## Purpose
Defines who may read what in the knowledge base: the mandatory owner filter
on every read, the principal captured after the auth gate that feeds it, the
guarantee that the owner is never LLM-controlled, and the accepted
same-text collision limitation.

## Requirements

### Requirement: Mandatory owner filter on every read

Every knowledge-base read — vector search, FTS search, document listing,
document deletion — SHALL take a `user_id` argument and filter on it at the
SQL level. The vec0 KNN query SHALL pre-filter on the owner auxiliary
column. There SHALL be no unfiltered search entry point in the store.

#### Scenario: Foreign chunks are invisible in search

- **WHEN** user B searches for text ingested only by user A
- **THEN** user B's results contain none of A's chunks

#### Scenario: No unfiltered read exists

- **WHEN** the store's public surface is inspected
- **THEN** every read method requires a `user_id` and applies it to the query

### Requirement: Principal captured after the auth gate

The acting user's Telegram numeric id SHALL be captured into the
`retrieval/principal.py` ContextVar immediately after the username allowlist
gate passes, and reset in a `finally` block. Because contextvars do not
cross the retrieval `ThreadPoolExecutor`, the id is passed down as an
explicit argument wherever threads are involved.

#### Scenario: The principal is the Telegram id

- **WHEN** an allowed user triggers a knowledge-base operation
- **THEN** the owner used for it is that user's numeric `from_user.id`, not the username

### Requirement: The owner is never LLM-controlled

The `kb_ingest` and `kb_search` tool schemas SHALL expose only
`{text, title}` and `{query}` respectively. Ownership SHALL NOT be a
parameter the LLM can set; it always comes from the principal.

#### Scenario: A tool call cannot claim another owner

- **WHEN** the LLM calls `kb_search` with a crafted query
- **THEN** the search is still scoped to the acting user because the tool schema has no owner field

### Requirement: Commands are scoped to the caller

`/documents` SHALL list only the invoking user's documents; `/delete` SHALL
cascade-delete a document's rows across all four tables in one transaction
only when the invoker owns it — a non-owner delete returns failure without
revealing whether the document exists.

#### Scenario: Deletion by a non-owner fails

- **WHEN** user B runs `/delete` on a document id owned by user A
- **THEN** the store reports failure and A's data is untouched

### Requirement: Same-text collision is last-writer

Document identity SHALL be a hash of the text, not of the owner: when two
users ingest the same text, the document row collides and the last writer
owns it. This is a documented limitation of the storage model, accepted
knowingly.

#### Scenario: Second ingest of the same text takes over the row

- **WHEN** user A ingests text T and user B later ingests the same text
- **THEN** the document row for T is owned by B; A's earlier copy does not create a second document

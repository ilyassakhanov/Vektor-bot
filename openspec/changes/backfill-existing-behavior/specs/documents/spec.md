# Delta: documents

## ADDED Requirements

### Requirement: Auth gate before any download

The username allowlist check SHALL run before a document is downloaded.
Documents from unauthorized users are never fetched from Telegram, never
extracted and never ingested.

#### Scenario: Unauthorized upload is not downloaded

- **WHEN** a user outside `ALLOWED_USERNAMES` sends a document
- **THEN** the bot issues no `get_file`/`download_file` call and the document branch ends at the gate

### Requirement: Bot-level ingestion before the agent

A message carrying a document (.txt/.md/.pdf/.docx) takes the document
branch (a message with both text and document takes this branch too). The
bot downloads the file — Telegram caps bot downloads at 20 MB, which is the
size limit — extracts pages via `documents.extract_pages`, and ingests them
with `KbIngestTool` BEFORE the agent runs. Rationale: extraction output can
be megabytes and SHALL NOT cross the LLM tool boundary as an argument. PDF
pages flow through so chunks carry page numbers.

#### Scenario: Extraction never becomes a tool argument

- **WHEN** a large PDF is uploaded
- **THEN** the agent receives a short synthetic upload notice, and the extracted text goes directly from the extractor to the store

### Requirement: Supported formats and error paths

Extraction supports .txt/.md (decoded as text), .pdf (per page via pypdf)
and .docx (paragraphs via python-docx), matched case-insensitively on the
filename extension. Unsupported or missing extensions, empty filenames and
corrupt bytes SHALL raise `DocumentError`.

#### Scenario: Unsupported extension is rejected

- **WHEN** a file named `report.xlsx` arrives
- **THEN** extraction raises `DocumentError` and the user gets one friendly reply

#### Scenario: Corrupt bytes are rejected

- **WHEN** a .pdf-named file contains bytes no PDF parser can read
- **THEN** extraction raises `DocumentError` instead of crashing polling

### Requirement: Staged progress replies

An upload SHALL produce staged replies in order: document received ->
extracting text -> generating embeddings -> ingested N chunks with the
document id and title -> document ready. The final agent reply (or the
friendly failure message) comes only after the staged replies.

#### Scenario: Progress precedes the outcome

- **WHEN** a document is uploaded and ingested successfully
- **THEN** the user sees the staged replies in order before the agent's response on the upload notice

### Requirement: The upload turn joins the conversation

The upload SHALL be recorded in the conversation context as a synthetic
notice naming the file and the ingested chunk count, with the user caption
appended when present. Follow-up questions in the same chat can therefore
`kb_search` the content. An upload without a caption gets an agent
acknowledgement of the notice.

#### Scenario: Follow-up questions see the upload

- **WHEN** the user asks a question after a successful upload
- **THEN** the conversation history contains the upload notice, and the agent can answer via `kb_search`

#### Scenario: A caption steers the acknowledgement

- **WHEN** the upload carries a caption
- **THEN** the synthetic notice includes it and the agent responds to notice plus caption

### Requirement: The upload flow never crashes polling

Any download, extraction or ingestion failure SHALL end with exactly one
friendly reply and no agent run. An LLM or agent error on the upload turn
produces the staged progress replies plus exactly one friendly reply. Logs
SHALL carry the file name and the outcome only — never file content or
captions.

#### Scenario: Ingestion failure yields one friendly reply

- **WHEN** the embedder fails during ingestion
- **THEN** the user sees the staged progress replies and exactly one friendly failure message, and polling continues

#### Scenario: Document logs stop at metadata

- **WHEN** a document fails extraction
- **THEN** the log names the file and the failure, without its content or caption

### Requirement: Source attribution

Answers based on retrieved chunks SHALL cite every fact as
`Источник: <filename>, стр. M` for PDF chunks or `Источник: <filename>,
chunk #N` when there is no page. When nothing relevant is found, the agent
SHALL say so explicitly instead of answering from general knowledge.

#### Scenario: A cited answer names the source

- **WHEN** the agent answers from a PDF chunk on page 3 of `report.pdf`
- **THEN** the answer cites `Источник: report.pdf, стр. 3`

#### Scenario: No hit is admitted

- **WHEN** `kb_search` returns no relevant chunks for the question
- **THEN** the agent states that nothing relevant was found rather than improvising

### Requirement: Documents require the KB

When the knowledge base is disabled (`KB_ENABLED=0`), a document upload
SHALL get the reply "Document uploads are not enabled." and nothing is
ingested.

#### Scenario: KB-disabled upload is declined

- **WHEN** the bot runs with `KB_ENABLED=0` and receives a document
- **THEN** the user gets the not-enabled reply and no ingestion happens

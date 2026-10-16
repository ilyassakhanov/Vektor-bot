# observability Specification

## Purpose
Defines what the system emits about itself: `vektor_*` Prometheus metrics
including retrieval instrumentation, JSON logs with redaction, and optional
Loki shipping — and the guarantee that sensitive content never appears in
metrics or logs.

## Requirements

### Requirement: LLM and tool instrumentation

The bot SHALL expose Prometheus metrics of the `vektor_*` family on
`/metrics` at `METRICS_PORT`: LLM token counts, latency, agent iterations,
tool calls and notional cost, recorded by instrumented wrappers around the
`LLM` interface and the tool registry. Notional cost prices are
configuration (`LLM_PRICE_IN_PER_1M` / `LLM_PRICE_OUT_PER_1M`), not
measurements — local Ollama costs $0.

#### Scenario: Metrics survive a provider swap

- **WHEN** the LLM implementation is replaced in the composition root
- **THEN** token, latency and iteration metrics keep flowing because the wrappers sit on the `LLM` interface

### Requirement: Retrieval instrumentation

The retriever SHALL record expansion status (`ok|fallback`), per-stage
latency (expansion, vector, fts, rerank, total), per-source result counts
(vector, fts, final) and rerank status (`ok|fallback`), with enum-only label
values.

#### Scenario: Retrieval fallbacks are visible

- **WHEN** rerank times out and falls back to RRF order
- **THEN** `vektor_retrieval_rerank_total{status="fallback"}` increments

### Requirement: Structured logs with redaction

Logs SHALL be JSON via `logging_config.configure_logging()` with a
`RedactionFilter`. Sensitive content — message text, prompts, LLM responses,
tool arguments and outputs, file contents, upload captions — SHALL NOT be
logged or metriced. Document handling logs the file name and the outcome
only.

#### Scenario: A message is never logged in clear

- **WHEN** a user sends a text message that is logged at debug level
- **THEN** the log record carries no readable message text

#### Scenario: Document logs stop at metadata

- **WHEN** a document fails extraction
- **THEN** the log names the file and the failure, without its content or caption

### Requirement: Optional Loki shipping

When `LOKI_PUSH_URL` is set, a `LokiPushHandler` SHALL batch log records and
push them to Loki; when unset, logs go to stderr only.

#### Scenario: No Loki URL means stderr only

- **WHEN** `LOKI_PUSH_URL` is empty
- **THEN** no Loki handler is attached and logging works unchanged

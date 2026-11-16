# Delta: message-io-safety

## MODIFIED Requirements

### Requirement: Busy-chat rejections are counted

A message rejected because its chat already has a run in flight SHALL be
counted in its own counter `vektor_chat_busy_total{path}` (`path` is one of
`text`, `upload`, `new`), with or without a per-user rate limiter configured.
It SHALL NOT be counted in `vektor_rate_limited_total`, whose reasons are only
`in_flight` and `window`.

#### Scenario: Busy-chat rejection is metriced

- **WHEN** a text message is rejected because another run is in flight in the same chat
- **THEN** `vektor_chat_busy_total{path="text"}` increases by one and `vektor_rate_limited_total` is unchanged

#### Scenario: Busy-chat upload is metriced by path

- **WHEN** a document upload is rejected because the chat is busy
- **THEN** `vektor_chat_busy_total{path="upload"}` increases by one

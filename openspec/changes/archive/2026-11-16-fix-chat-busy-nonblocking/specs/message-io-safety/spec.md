# Delta: message-io-safety

## ADDED Requirements

### Requirement: Busy-chat rejections are counted

A message rejected because its chat already has a run in flight SHALL be
counted in `vektor_rate_limited_total{reason="chat_in_flight"}`, with or
without a per-user rate limiter configured.

#### Scenario: Busy-chat rejection is metriced

- **WHEN** a message is rejected because another run is in flight in the same chat
- **THEN** `vektor_rate_limited_total{reason="chat_in_flight"}` increases by one

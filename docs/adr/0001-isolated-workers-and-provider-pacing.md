# ADR 0001: Isolated workers and provider pacing

Status: Accepted for version 0.1

## Context

Telemetry, tariff collection, forecast planning, reconciliation, and the dashboard have
different schedules and failure modes. A combined scheduler couples those failures, creates
bursty multi-property traffic, and makes least-privilege operation harder. Planning must also
remain deterministic: it must use the tariff snapshot already recorded in InfluxDB rather than
making an untracked supplier request.

## Decision

Run five independent, long-lived processes: telemetry, tariff, forecast-plan, reconciliation,
and dashboard. Each process owns its InfluxDB client. Provider-facing workers reuse one bounded
HTTP session and one `RequestPacer`; every outbound provider request is serialized, separated by
a configurable minimum interval, and retried only for transport errors, HTTP 429, and selected
5xx responses. Retry count, exponential backoff, jitter, and `Retry-After` are bounded.
Provider `Retry-After` values have their own configurable inline-wait maximum. A longer value is
never shortened: the current cycle aborts and the pacer rejects later cycles without making an
outbound request until the provider's full delay has elapsed.
Responses are consumed as streams and rejected when their decompressed JSON exceeds the
configurable byte ceiling or fixed structural limits. Octopus intervals must form one half-open,
non-overlapping timeline: exact boundaries are accepted, while overlaps fail closed at collection
and again before planning stored data. Sigenergy's nested `data` encoding has tighter wrapper,
byte, object-shape, node, and depth bounds before telemetry normalization.

Properties run sequentially with configurable phase spacing. Forecast-plan and reconciliation
scan for due work, catch up missed runs, and check durable InfluxDB or local outbox markers before
writing.
Forecast-plan reads a complete, fresh stored tariff window and never calls Octopus. Log messages
identify only the operation, configured property ID, and exception class; provider URLs,
coordinates, system IDs, and response bodies are excluded.

## Consequences

- A failed worker does not restart or stop another worker.
- Each command needs a separate service unit and database connection.
- Each command has a distinct Unix user, private group, and root-owned environment file. A shared
  read-only group exposes only the non-secret YAML. Configuration loading selects its token first
  and strips unrelated provider credential fields before environment expansion.
- Telemetry, tariff, and planning data use separate buckets because InfluxDB authorization is
  bucket-scoped. Worker tokens receive only their required bucket operations.
- Tariff collection must run often enough to satisfy the configured freshness bound.
- InfluxDB remains the inter-worker handoff boundary. Each writing worker uses the private fallback
  outbox specified by ADR 0002 after a failed or ambiguous direct attempt, and queues behind an
  existing same-property backlog. Pending local logical markers extend the writer's idempotency
  boundary across an InfluxDB outage.
- Catch-up work increases request volume gradually because pacing and property phasing still apply.
- Version 0.1 remains recommendation-only; none of these workers can control a battery.

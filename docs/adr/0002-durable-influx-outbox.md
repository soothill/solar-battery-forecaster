# ADR 0002: Durable InfluxDB write outbox

Status: Accepted for version 0.1

## Context

Synchronous InfluxDB writes alone cannot distinguish a request that never arrived from one that
was committed before a timeout. They also lose newly collected telemetry, tariffs, forecasts, or
planning results when InfluxDB is unavailable. Refetching a forecast after an ambiguous write can
mix forecast vintages and change the associated correction factor.

## Decision

Each of the four writing processes owns a private SQLite database at
`$OUTBOX_STATE_DIRECTORY/outbox.sqlite3`. The dashboard remains read-only and has no outbox. A
writer converts the complete batch once to deterministic Influx line protocol with second-precision
timestamps. If that property's stream is healthy and the worker circuit is open, it first attempts
a synchronous direct InfluxDB write. Confirmed success creates no payload row. A failed or ambiguous
attempt immediately commits the exact attempted bytes to SQLite, together with their organization,
bucket, worker, property, schema version, logical identity, time range, length, SHA-256, metadata,
and domain-separated event ID. If the same property already has a valid pending record, the new
batch is committed behind it without bypassing FIFO. A quarantined or permanently blocked stream
rejects new collection for that property until explicit repair; its bytes are never treated as a
replayable predecessor. A healthy peer property can continue direct writes unless a worker-wide
retry or authorization circuit is active.

SQLite uses WAL, `synchronous=FULL`, foreign keys, secure deletion, bounded checkpoints, and
mode-0600 files inside a mode-0700 state directory. The fallback durability guarantee begins only
after that transaction commits. A process or host crash before the fallback commit can lose the local copy
and a crash during the direct request can leave the InfluxDB outcome unknown. This accepted window
is the cost of avoiding a persistent disk write for every healthy telemetry sample.

The schema and small control database may exist during healthy operation because it retains pacing,
counter, and health metadata. Confirmed direct success creates no payload row. Private writer state
is not readable by the dashboard: charts therefore show only confirmed InfluxDB data, while the
writer-scoped outbox status command is authoritative for pending local data.

Only a confirmed synchronous InfluxDB response removes a record. A timeout, disconnect, partial
response, or crash before local acknowledgement leaves the whole persisted batch for replay. The
Influx measurement, complete tag set, and timestamp are unchanged, so replay is an idempotent
upsert. No queued or quarantined record expires or is evicted automatically.

Replay is bounded and runs before the first collection and at every cycle; fallback enqueue does not
immediately retry. Only the head of each property stream is eligible, and streams rotate after every
attempt to preserve FIFO order and multi-property fairness. A delivery failure establishes one
persisted, bounded exponential retry delay for the writer, preventing outage request storms.
Authorization failure pauses delivery worker-wide but still permits capacity-safe buffering.
Record-specific permanent failures block only that property stream. Before calling a provider, a
worker checks record, database-byte, journal-headroom, collection-reserve, and filesystem free-space
bounds. Failed admission suppresses collection rather than collecting data it cannot retain after a
delivery failure.

Forecast records retain the summary required to reconstruct the exact pending snapshot. Pending or
quarantined decision and daily-result logical markers count as existing work. A valid pending
forecast is reused without provider refetch or factor recomputation; a checksum-damaged stream is
blocked before provider access. Checksum failures atomically preserve the record in quarantine and
block only its property stream. SQLite structural corruption fails the entire writer closed and
never replaces or deletes the database or its sidecars.

Operators use the scoped `outbox status`, `verify`, `drain`, `retry`, and `export-quarantine`
commands. Quarantine exports contain line protocol and property identifiers, are created mode 0600,
and must be treated as private operational data. Retry resets delivery pacing only; it does not
silently restore corrupt quarantined bytes.

## Consequences

- The four writer units need separate persistent state directories and mode-0077 umasks.
- Disk sizing and free-space monitoring are part of deployment acceptance.
- InfluxDB downtime increases local disk usage; collection stops visibly at configured bounds.
- SQLite schema changes require explicit forward migrations and rollback testing.
- Rollback must preserve the state directory and use a binary that understands its schema.
- At-least-once fallback replay can repeat a confirmed Influx write after a crash, but cannot alter
  the deterministic point identity. Direct-first delivery retains the disclosed pre-commit window.

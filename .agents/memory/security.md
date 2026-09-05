# Security memory

- 2026-09-05: Property energy data can reveal occupancy. Never expose coordinates, system IDs,
  account identifiers, serial numbers, or raw payloads in code, logs, fixtures, or memory.
- 2026-09-05: Sigenergy developer AppKey and a bucket-scoped Influx token are separate read-only
  collector credentials. Future control requires a separate process and credential.
- 2026-09-05: One operator may configure several properties. Hosted untrusted tenants require a new
  authorization and storage-isolation design.


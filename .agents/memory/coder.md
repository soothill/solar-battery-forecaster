# Coder memory

- 2026-09-05: Python 3.11+, Pydantic configuration, HTTPX adapters, APScheduler, InfluxDB v2 client.
- 2026-09-05: Config and environment files containing real credentials are gitignored.
- 2026-09-05: Dependencies are locked in `uv.lock`; CI uses frozen installs.
- 2026-09-05: Forecast writes use issued-time snapshot tags; readers accept only a complete
  local-day snapshot so interrupted retries cannot mix forecast vintages.
- 2026-09-05: Vendor implementations sit behind typed adapter protocols and named factories;
  adding a provider should not change the collector's planning flow.
- 2026-09-05: Every battery recommendation persists the forecast snapshot ID and issue time,
  SoC observation time, and exact tariff coverage window so decisions remain auditable.
- 2026-09-05: Long-running telemetry, tariff, forecast-plan, reconciliation, and dashboard
  processes are failure-isolated. Provider workers reuse one HTTP session and serialize outbound
  requests; InfluxDB is the durable handoff and idempotency boundary.
- 2026-09-05: When multiple complete forecasts exist for a local day, planning deterministically
  uses the newest issued snapshot. Provider `Retry-After` uses a separate inline-wait limit from
  ordinary backoff; longer values defer outbound calls for the full provider-requested delay.
- 2026-09-05: Forecast planning becomes due only after today's local scheduled time and targets
  tomorrow; it never creates a late same-day plan. Command-scoped config strips unrelated provider
  secrets before expansion and selects a process-specific token for bucket-level least privilege.
- 2026-09-05: One-shot worker runs aggregate isolated property/due-job failures into a nonzero exit,
  while long-lived workers continue subsequent cycles. Dashboard forecast reads use the same newest
  complete issued snapshot as planning and reconciliation.
- 2026-09-05: Runtime least privilege requires three explicit Influx buckets, distinct Unix
  user/private-group identities and secret files per process, and an exactly pinned build backend
  installed from the frozen development lock before no-isolation builds.
- 2026-09-05: The three Influx bucket names must be pairwise distinct. Releases originate only from
  exact version tags whose verified commit is reachable from protected main; frozen artifacts and
  their checksum file receive GitHub/Sigstore provenance attestations, which deployment verifies
  against the exact workflow, tag, and reviewed source digest before treating checksums as useful.
- 2026-09-05: Provider JSON is consumed as a bounded decompressed stream and validated for
  structural complexity before adapter parsing. Octopus intervals form one validated,
  non-overlapping timeline for coverage, duration, and weighted-price calculations; Sigenergy's
  nested data encoding has additional string, wrapper, shape, node, and depth limits.
- 2026-09-05: In a 512 MB LXC, the four workers use 80 MB `MemoryMax` limits and the dashboard uses
  96 MB, leaving 96 MB outside service ceilings for the base system.
- 2026-09-05: The `ic-dev` runner deployment uses a root-owned systemd timer, encrypted GitHub App
  credential, short-lived installation token, one-use JIT configuration, serialized one-job
  container, disposable work volume, digest-addressed images, and validated proxy/firewall
  lifecycle. Installation remains inactive until live isolation and egress acceptance pass.
- 2026-09-05: Normal CI is anchored in the protected base workflow. An immutable origin condition
  admits only same-repository heads to the read-only self-hosted quality job, and a separate hosted
  no-checkout publisher uses a distinct protected-environment, status-only App to write final
  App-bound required statuses to the exact head. All external fork workflows require approval and
  must never be approved. Runner activation also
  requires a fingerprint-bound executable host/egress/cleanup acceptance marker; failed JIT starts
  are paced and use a fresh installation token for best-effort stale-registration cleanup. Trusted
  tag release provenance remains GitHub-hosted.

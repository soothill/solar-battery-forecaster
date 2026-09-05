# Architect memory

- 2026-09-05: Target runtime is an unprivileged Debian LXC. Existing storage is InfluxDB OSS 2.8.
- 2026-09-05: Use vendor-neutral forecast, inverter, tariff, and storage adapters.
- 2026-09-05: Preserve the exact forecast used for the overnight decision; later refreshes must not
  overwrite it.
- 2026-09-05: All storage timestamps are UTC; property time zones define local schedules/days.


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

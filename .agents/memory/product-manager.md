# Product Manager memory

- 2026-09-05: Primary outcome is a mobile-friendly single view overlaying forecast and actual PV
  generation plus planned and actual battery SoC.
- 2026-09-05: Show Octopus cheap/dispatch windows, issued-at/freshness, correction factor, daily
  totals, recommended target, and charge cost. The York curve is the visual reference.
- 2026-09-05: Support self-hosted multi-property configuration; hosted tenant isolation is not yet
  supported.
- 2026-09-05: Failed or backlogged InfluxDB writes need a visible bounded fallback queue that avoids
  forecast refetch and recomputation. The direct-first design intentionally cannot guarantee a local
  copy if the process crashes before the fallback commit.

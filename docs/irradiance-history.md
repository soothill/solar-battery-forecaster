# Forecast irradiance and satellite estimates

Forecast capture starts with a new snapshot fetched by this version. Earlier forecasts cannot be
retroactively reconstructed from archives. An external historical weather-model series is not
measured actual irradiance. This worker collects **satellite-derived estimates**, not readings
from a property sensor. Inverter generation and automatic calibration remain unchanged. A ratio
from mismatched locations or periods is not a calibration result. Comparisons require validated
site coordinates, array tilt, compass azimuth, matching UTC intervals, geometry, coverage and the
original forecast snapshot.

Check the [satellite API documentation](https://open-meteo.com/en/docs/satellite-radiation-api),
[licence and terms](https://open-meteo.com/en/terms), and
[quota/commercial requirements](https://open-meteo.com/en/pricing) before enabling this public
endpoint. The integration has no paid endpoint/API-key mode. Location queries leave the host for
`satellite-api.open-meteo.com` over HTTPS. Geometry digests do not anonymize location. Keep config,
outboxes and database exports private. Satellite delay and spatial/model limitations mean these
estimates do not prove roof-level ground truth.

Set `irradiance_history.enabled: true` for validated properties; default is false.
`catch_up_days` defaults to 7 (range 1–30). Hourly scans refresh each completed UTC day once per
UTC refresh day. Each day requires one request per array, returning two calendar days to include
the final midnight sample: normally seven requests per array daily, plus bounded retries. Sparse
coverage is recorded and retried next daily refresh. Days outside the rolling window are not
automatically refreshed. Requests share existing pacing, response bounds and Retry-After handling.

`shortwave_radiation` is GHI and `global_tilted_irradiance` is GTI, mean W/m² over the preceding
hour. Compass azimuth converts to south-zero by subtracting 180°. Stored timestamps identify the
UTC interval start; UTC history days remain 24 hours across local DST changes. Zero is valid;
missing values remain missing. Never sum GHI across arrays: it is repeated for traceability.

`irradiance_forecast` accompanies `pv_forecast` in the same durable batch, with the same issued-time
snapshot tag. It stores uncorrected `ghi_w_m2`/`gti_w_m2`, validity, geometry, orientation, duration,
unit and `issued_at` (fetch time). Inverter clipping does not change raw irradiance. Legacy
snapshots lack this measurement and remain unchanged when reused.

`irradiance_history` stores separate `metric=ghi` and `metric=gti` points with `value_w_m2`, unit,
duration and `collected_at`. Valid revisions overwrite the same identity. Null refreshes retain
older estimates and their old collection time. `irradiance_history_collection` stores the latest
`valid_hours`, `expected_hours`, `coverage`, `complete`, `refresh_day` and `collected_at` per metric,
array, geometry and UTC day. Inspect coverage before comparison; counting retained rows does not
prove latest-response completeness. Collection tags grow once per property/geometry-set/day;
set planning retention deliberately, balancing comparison history and storage/cardinality.

Example Flux for a captured forecast; substitute private identifiers locally:

```flux
from(bucket: "solar_planning")
  |> range(start: 2026-10-20T00:00:00Z, stop: 2026-10-21T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "irradiance_forecast")
  |> filter(fn: (r) => r.property == "example-home" and r.array == "south-roof")
  |> filter(fn: (r) => r.geometry == "GEOMETRY_ID" and r.snapshot == "SNAPSHOT_ID")
  |> filter(fn: (r) => r._field == "gti_w_m2")
```

For satellite comparison use the same property/array/geometry and UTC range, measurement
`irradiance_history`, `provider=open_meteo_satellite`, `model=satellite_radiation_seamless`,
`source_kind=satellite_derived`, `metric=gti`, `_field=value_w_m2`, and inspect collection coverage
for the same day. Irradiance is not generated kWh.

The worker sends batches directly to InfluxDB first. Failure, ambiguity or existing property
backlog commits exact bytes to private SQLite; pending markers prevent refetch while offline.
Restart replays FIFO before due checks. Full, quarantined or blocked fallback remains visible
through shared admission controls and health. Standard `validate`, `outbox` and `--once`
maintenance templates accept scope `irradiance-history`. Stopping the worker stops collection;
existing data remains readable. See [LXC installation](../deployment/LXC.md) and
[setup](setup-and-credentials.md). No deployment is performed by adding this code.

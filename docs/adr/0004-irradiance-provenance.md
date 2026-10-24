# ADR 0004: forecast irradiance and separate satellite history

Preserve original per-array GHI and GTI alongside forecast snapshots, and collect historical
satellite-derived estimates independently. Neither can be reconstructed from generated energy.
`ForecastInterval.irradiance` is a tuple of frozen `ArrayIrradiance` values, default `()` for older
adapters. Each includes array name, compass azimuth, tilt, nullable GHI/GTI and a geometry digest
incorporating configured location and orientation. The digest is not anonymization. Missing GHI
does not invalidate usable PV forecasts. All fetched raw intervals share one serialized batch and
issued-time snapshot with selected `pv_forecast` intervals. `issued_at` means fetch time, not
model-run time. Reusing a stored snapshot never attaches newer raw data.

Raw transport is an independent tuple of frozen `ForecastIrradianceInterval` records without PV
fields. It includes hours with missing GTI, even outside required planning coverage. Such hours
never become zero or partial-roof PV predictions. The adapter clears this tuple before each fetch
and publishes it only after validation succeeds; storage rejects a different provider/issue time.

`irradiance_history` has stable property/array/geometry/provider/model/source_kind/metric tags and
target-hour timestamps. `source_kind=satellite_derived` is explicit; there is no site sensor.
Non-null estimates revise the same identity. `collected_at` is a field. Null refreshes retain
earlier values and their original collection times. Separate latest-refresh
`irradiance_history_collection` coverage reports valid/expected hours per metric/array/day, so
retained old values never imply fresh completeness. History never changes forecasts, inverter
generation, calibration or battery recommendations.

The dedicated Open-Meteo satellite archive uses explicit `models=satellite_radiation_seamless`.
Requests include UTC day D+1 to capture the midnight sample covering D's last hour. Values mean
the preceding hour, so target timestamps shift back one hour. Each batch describes 24 UTC slots
per array, with missing samples nullable. Units, shape, numeric bounds, duplicates, alignment and
UTC are validated within the existing bounded-response and paced retry mechanism.

The hourly worker refreshes seven completed UTC days once per UTC refresh day (range 1–30).
Each property/day is one exact-byte batch including coverage markers. Collection identity includes
provider/model and the complete array/geometry set. Its marker records refresh date; the same
fallback logical key blocks refetch across midnight and restart until replay. Later daily refreshes
revise the day, including previously partial days. Database read failure fails the due scan rather
than assuming absence. Existing fallback admission, FIFO replay, quarantine and health rules apply.

The opt-in worker has planning-bucket read/write only, separate credentials, Unix identity,
private outbox and status paths. Dashboard health expects it only when enabled. Its 80 MB ceiling
raises the total to 496 MB; enabling requires increased LXC capacity and validated headroom.
No dependencies or migration are required. Existing generation and forecast schemas remain valid.

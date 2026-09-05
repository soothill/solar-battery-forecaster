# Solar Battery Forecaster

Solar Battery Forecaster is a self-hosted service that records rooftop solar forecasts,
actual inverter generation, battery telemetry, and Octopus Energy prices in InfluxDB. It
learns an actual-to-forecast correction factor and logs a conservative overnight battery
charge recommendation.

The project is designed for a small Debian/Ubuntu LXC and for more than one property.
Home Assistant is not required.

> [!IMPORTANT]
> Version 0.1 is **recommendation-only**. It never changes inverter or battery settings.
> Control will be added only after the read and planning paths have been validated against
> real hardware.

## Current support

| Capability | Adapter | Status |
|---|---|---|
| Solar forecast | Open-Meteo tilted irradiance | Implemented |
| Inverter telemetry | Sigenergy Cloud OpenAPI | Implemented, requires developer AppKey |
| Electricity prices | Octopus Energy REST API | Implemented |
| Intelligent Octopus bonus dispatches | Octopus GraphQL `flexPlannedDispatches` | Planned |
| Time-series storage | InfluxDB OSS 2.x | Implemented |
| Overnight target | Conservative energy balance | Implemented, recommendation-only |
| Battery control | Vendor-specific adapters | Deliberately disabled |

The Sigenergy adapter uses the official developer AppKey flow. Apply through the
[Sigen developer portal](https://developer.sigencloud.com/). Each user supplies their own
credentials; credentials are never stored in this repository.

## How it works

At five-minute intervals the service reads PV power, battery state of charge, load, grid
flow, and cumulative generation. Before the overnight period it preserves the next day's
forecast, calculates a robust correction factor from completed days, and writes a battery
target recommendation. At the end of a day it records actual versus forecast generation.

All timestamps are written in UTC. Property time zones are used only to define local days
and schedules, which keeps half-hourly data correct over daylight-saving changes.

## InfluxDB measurements

- `energy_telemetry`: five-minute Sigenergy readings.
- `pv_forecast`: the immutable overnight forecast, timestamped by target interval.
- `electricity_tariff`: Octopus price intervals and a derived `is_cheap` field.
- `battery_decision`: recommendation inputs, charge-window cost, and output;
  `automation_enabled` is always false.
- `pv_daily`: forecast, actual, error, daily ratio, and learned correction factor.

Tags are intentionally limited to stable values such as property, provider, and source to
avoid high series cardinality. Addresses, account numbers, API keys, serial numbers, and
installation IDs are not tags and are not written by the supplied adapters.

## Quick start

Requires Python 3.11 or later and InfluxDB OSS 2.x.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
cp .env.example .env
```

Load the environment variables using your preferred secret manager, edit `config.yaml`,
then validate the database connection:

```bash
set -a
. ./.env
set +a
solar-battery-forecaster validate --config config.yaml
solar-battery-forecaster collect-once --config config.yaml
```

Run continuously:

```bash
solar-battery-forecaster run --config config.yaml
```

## Configuration

Copy `config.example.yaml`; do not edit the example in place. Secret values use exact
environment-variable placeholders such as `${INFLUX_TOKEN}`. Startup fails if a required
variable is absent.

Each roof plane has its own panel count, panel wattage, tilt, compass azimuth, and initial
performance ratio. Compass azimuth uses the familiar convention: north 0°, east 90°,
south 180°, and west 270°.

The example describes a 6 kW Sigenergy inverter and a 9 kWh usable battery. Change the
array layout and location before using its results.

## InfluxDB permissions

Create a dedicated bucket, normally `solar_planner`, and a token with read/write access to
that bucket only. Do not use an all-access operator token. The service reads its own daily
results to learn the correction factor.

## Octopus prices and charging intervals

The public Octopus tariff endpoint returns price validity intervals. The service marks an
interval as cheap when its inclusive-of-VAT price is at or below the configured threshold.
This works for fixed off-peak and dynamic tariffs without hard-coding clock times.

Extra Intelligent Octopus dispatches outside the normal cheap window require an
authenticated GraphQL integration. Octopus deprecated `plannedDispatches` in favour of
`flexPlannedDispatches`; that integration is intentionally not guessed in the first
release and will be added against a real account fixture.

## LXC deployment

See [`deployment/LXC.md`](deployment/LXC.md). A native systemd service is used rather than
running Docker inside LXC.

## Mobile dashboard

The included phone-friendly dashboard puts the day's solar generation and battery state of
charge in one chart. Dashed lines show the preserved conservative forecast and planned SoC; solid lines
overlay actual inverter generation and actual SoC. Cheap Octopus intervals are shaded, and
the forecast issue time remains visible so stale data is obvious. The same view shows the
recommended target, grid charge, estimated cost, and learned correction factor, with a property
selector for multi-property installations.

Run `solar-battery-dashboard --config config.yaml`, then open
`http://127.0.0.1:8080/?property=example-home`. For use away from the LXC itself, keep the
service on loopback and publish it through an authenticated HTTPS reverse proxy.

## Safety and privacy

- Battery control is not implemented in version 0.1.
- API tokens and property configuration are gitignored.
- Public examples use fictional coordinates and identifiers.
- Forecast failure cannot reduce a real battery target because the service cannot control it.
- A future control release must include stale-data checks, reserve enforcement, explicit
  opt-in, dry-run logging, and a safe fallback target.

## Development

```bash
pip install -e '.[dev]'
ruff check .
pytest
```

Contributions for additional inverter, forecast, and tariff adapters are welcome. Keep
vendor payloads inside adapters and return the common models from `models.py`.

## License

MIT

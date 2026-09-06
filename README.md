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

At five-minute intervals the telemetry worker reads PV power, battery state of charge, load, grid
flow, and cumulative generation. Before the overnight period it preserves the next day's
forecast, calculates a robust correction factor from completed days, and writes a battery
target recommendation. At the end of a day it records actual versus forecast generation.

Telemetry, tariff, forecast planning, reconciliation, and the dashboard run as independent
processes. A failure in one does not restart the others. Provider sessions are reused and all
outbound requests are serialized, paced, and retried within configured bounds. Forecast planning
uses only fresh Octopus intervals already stored by the tariff worker. Provider JSON is streamed
through a decompressed-byte and structural-complexity limit; ambiguous overlapping tariff
intervals are rejected before they can affect charge duration or price calculations.

Each writer normally sends exact InfluxDB line protocol directly. After a failed or ambiguous
attempt, or while that property is already backlogged, it commits the same bytes to its bounded
SQLite fallback outbox for idempotent replay. Collection stops visibly before disk reserves are
breached, with no silent eviction. A healthy process can have an empty SQLite control database,
but a confirmed direct success creates no payload row. The dashboard remains read-only and charts
only data confirmed in InfluxDB; the scoped outbox CLI is authoritative for local backlog state.

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

For a complete Proxmox LXC walkthrough—including InfluxDB buckets/tokens, Sigenergy and Octopus
onboarding, per-service secrets, firewalling, authenticated mobile access, acceptance tests,
backups, rotation and troubleshooting—start with the
**[Setup and credentials guide](docs/setup-and-credentials.md)**. The hardened release-install
commands remain canonical in [`deployment/LXC.md`](deployment/LXC.md).

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
```

Edit `config.yaml` and copy the relevant examples from `deployment/environment/`. Do not shell-load
these files: provider values are data and can contain shell metacharacters. The hardened LXC
installation uses systemd `EnvironmentFile=` directives and direct process execution. For real
credentials and scoped validation, follow the [Setup and credentials guide](docs/setup-and-credentials.md).

```bash
cp deployment/environment/telemetry.env.example telemetry.env
```

For continuous operation, run each command without `--once` in its own process. The supplied
systemd units do this and intentionally have no failure coupling.

The process boundary, pacing, and durable-delivery decisions are recorded in
[`docs/adr/0001-isolated-workers-and-provider-pacing.md`](docs/adr/0001-isolated-workers-and-provider-pacing.md)
and [`docs/adr/0002-durable-influx-outbox.md`](docs/adr/0002-durable-influx-outbox.md).

## Configuration

Copy `config.example.yaml`; do not edit the example in place. Secret values use exact
environment-variable placeholders such as `${INFLUX_TELEMETRY_TOKEN}`. Startup fails if a
variable required by the selected command is absent; unrelated provider secrets are neither
required nor expanded.

Each roof plane has its own panel count, panel wattage, tilt, compass azimuth, and initial
performance ratio. Compass azimuth uses the familiar convention: north 0°, east 90°,
south 180°, and west 270°.

The example describes a 6 kW Sigenergy inverter and a 9 kWh usable battery. Change the
array layout and location before using its results.

## InfluxDB permissions

Use separate telemetry, tariff, and planning buckets because InfluxDB OSS permissions are
bucket-level. All three bucket names are required; the pre-release configuration deliberately
rejects the legacy single `bucket` setting because it defeats process isolation. Give each worker
its own token with only the bucket reads/writes listed in
[`deployment/LXC.md`](deployment/LXC.md); never use an all-access operator token. Command-scoped
configuration prevents a worker from resolving unrelated provider credentials or tokens.

## Octopus prices and charging intervals

The public Octopus tariff endpoint returns price validity intervals. The service marks an
interval as cheap when its inclusive-of-VAT price is at or below the configured threshold.
This works for fixed off-peak and dynamic tariffs without hard-coding clock times.

Version 0.1 accepts only positive price intervals of at most two hours. Some real Intelligent
Octopus responses contain 6-hour or 18-hour standard-rate intervals and are therefore rejected.
The Intelligent tariff codes in `config.example.yaml` are illustrative, not a production-tested
default; test the property's exact codes with `tariff --once` before relying on them.

Extra Intelligent Octopus dispatches outside the normal cheap window require an
authenticated GraphQL integration. Octopus deprecated `plannedDispatches` in favour of
`flexPlannedDispatches`; that integration is intentionally not guessed in the first
release and will be added against a real account fixture.

## LXC deployment

See [`deployment/LXC.md`](deployment/LXC.md). Native, hardened systemd services are used rather
than running Docker inside LXC. The release wheel and source archive are checksum-verified, and
the privileged installation path never resolves or runs an unpinned build backend.

## Mobile dashboard

The included phone-friendly dashboard puts the day's solar generation and battery state of
charge in one chart. Dashed lines show the preserved conservative forecast and planned SoC; solid lines
overlay actual inverter generation and actual SoC. Cheap Octopus intervals are shaded, and
the forecast issue time remains visible so stale data is obvious. The same view shows the
recommended target, grid charge, estimated cost, and learned correction factor, with a property
selector for multi-property installations.

The supplied systemd unit runs the dashboard on `127.0.0.1:8088`; open
`http://127.0.0.1:8088/?property=example-home` inside the LXC. The standalone
`solar-battery-dashboard --config config.yaml` command defaults to port 8080 unless
`--port 8088` is supplied. For use away from the LXC itself, keep the service on loopback and
publish it through an authenticated HTTPS reverse proxy.

The page also has a live status view for all five independent processes, with 30-second
heartbeats, last cycle/accepted/confirmed-delivery times, fallback counts, and a bounded list of
fixed operational events. `GET /api/status` exposes the same no-store JSON. The dashboard reads only
fixed group-readable snapshots under `/run`, never another process's SQLite fallback or journal.

Optional remote syslog supports UDP, TCP, and certificate-verified TLS. It is disabled by default
and sends only the fixed operational event codes—never arbitrary application log messages or
property identifiers. Its bounded background queue cannot delay collection. See
[`deployment/LXC.md`](deployment/LXC.md) for permissions and firewall guidance.

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

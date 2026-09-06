# Setup and credentials guide

Last verified: 2026-09-06

This guide takes an operator from an empty Proxmox container to a running,
recommendation-only Solar Battery Forecaster installation. It also explains where each required
value comes from, which process receives it, and how to prove that the installation works without
putting credentials or property data in the repository.

> [!IMPORTANT]
> Version 0.1 is pre-production and cannot control a battery or inverter. It records data and
> produces a recommendation only. Complete the acceptance checklist in this guide with the real
> property, providers, network controls, and InfluxDB instance before relying on its output.

The hardened commands and release-verification requirements in
[`deployment/LXC.md`](../deployment/LXC.md) are canonical. This guide explains the end-to-end
operator journey; where the two documents overlap, follow the stricter requirement in the LXC
deployment reference.

## What you need before starting

- Proxmox VE access that can create an unprivileged Debian 12 LXC.
- A DNS name or fixed address for the existing InfluxDB OSS 2.x service.
- An InfluxDB administrator who can create three buckets and five custom tokens.
- The property's time zone, latitude and longitude, and each roof plane's tilt and compass
  azimuth (north 0°, east 90°, south 180°, west 270°).
- Panel count and rated watts for every roof plane, inverter rated power, battery usable capacity,
  reserve, charge power, and expected efficiency.
- Access to the property's Sigenergy installation and an approved Sigenergy developer
  application.
- The exact Octopus product and electricity tariff codes from the property's account/contract.
- A password manager or secrets vault. Do not store credentials in Git, tickets, chat, shell
  history, or the shared YAML configuration.

Use a short, non-identifying property alias such as `home-01`. Do not use an address, postcode,
customer name, account number, serial number, or provider installation identifier as the alias.

## Architecture at a glance

The LXC runs five independent systemd services:

| Service | Purpose | Writes when InfluxDB is unavailable |
|---|---|---|
| `solar-battery-telemetry` | Sigenergy generation, load, grid and battery telemetry | Its private SQLite outbox |
| `solar-battery-tariff` | Octopus price intervals | Its private SQLite outbox |
| `solar-battery-forecast-plan` | Open-Meteo forecast and charge recommendation | Its private SQLite outbox |
| `solar-battery-reconciliation` | Actual-versus-forecast result and correction factor | Its private SQLite outbox |
| `solar-battery-dashboard` | Read-only mobile dashboard and process status | Nothing |

Each writer sends its exact batch directly to InfluxDB first. SQLite is used only after a failed or
ambiguous InfluxDB write, or while that property's stream already has a backlog. After InfluxDB
recovers, the persisted bytes are replayed idempotently. SQLite is not the primary database, and
the dashboard intentionally shows only data confirmed in InfluxDB.

## Provider and secret inventory

Create the following inventory in your password manager. The table describes current version 0.1
behavior, not planned integrations.

| Input | Exact file and variable | Authoritative source | Secret? | Network destination | Minimum privilege | How to validate | Rotation |
|---|---|---|---:|---|---|---|---|
| Sigenergy AppKey | `telemetry.env`: `SIGENERGY_HOME_APP_KEY` | Sigenergy developer portal | Yes | Selected regional `openapi-*.sigencloud.com` host | Approved **Monitoring** application only; no Control or VPP dispatch | Run telemetry once and confirm a new `energy_telemetry` point | Create/rotate in the portal, update only `telemetry.env`, test, then retire the old credential if the portal permits overlap |
| Sigenergy AppSecret | `telemetry.env`: `SIGENERGY_HOME_APP_SECRET` | Shown once when generated in the developer portal | Yes | Same as AppKey | Same Monitoring application | Same telemetry test | Store immediately; schedule a maintenance window if regeneration invalidates the old secret immediately |
| Sigenergy system ID | `telemetry.env`: `SIGENERGY_HOME_SYSTEM_ID` | Authorized system/site shown by the portal or returned by its documented system API | Treat as sensitive | Same as AppKey | Only the authorized property | Same telemetry test | Replace if the installation/system identity changes |
| Octopus product and tariff codes | `config.yaml`: `product_code`, `tariff_code` | Property's Octopus account/contract and public product API | No, but account context is private | `api.octopus.energy:443` | Public standard-unit-rate endpoint | Run tariff once and confirm `electricity_tariff` points with expected dates/prices | Recheck whenever the tariff changes |
| Octopus account API key | No version 0.1 variable or file | Octopus account Developer settings | Yes | Not used by version 0.1 | Do not install it for the current REST adapter | Not applicable | Manage in the Octopus account when a future authenticated adapter explicitly requires it |
| Open-Meteo forecast | No version 0.1 variable or file | Public non-commercial API | No key in version 0.1 | `api.open-meteo.com:443` | Public forecast endpoint | Run forecast-plan when due and confirm a complete `pv_forecast` snapshot | Not applicable |
| Telemetry Influx token | `telemetry.env`: `INFLUX_TELEMETRY_TOKEN` | InfluxDB administrator | Yes | Configured InfluxDB HTTPS endpoint | Write `solar_telemetry` only | Validate scope, run telemetry and confirm a point | Replace, test this service, then revoke old token |
| Tariff Influx token | `tariff.env`: `INFLUX_TARIFF_TOKEN` | InfluxDB administrator | Yes | Configured InfluxDB HTTPS endpoint | Write `solar_tariff` only | Validate scope, run tariff and confirm points | Replace, test this service, then revoke old token |
| Forecast-plan Influx token | `forecast-plan.env`: `INFLUX_FORECAST_PLAN_TOKEN` | InfluxDB administrator | Yes | Configured InfluxDB HTTPS endpoint | Read all three buckets; write `solar_planning` | Validate scope, run a due plan and confirm snapshot/decision | Replace, test this service, then revoke old token |
| Reconciliation Influx token | `reconciliation.env`: `INFLUX_RECONCILIATION_TOKEN` | InfluxDB administrator | Yes | Configured InfluxDB HTTPS endpoint | Read telemetry/planning; write planning | Validate scope, run due reconciliation and confirm `pv_daily` | Replace, test this service, then revoke old token |
| Dashboard Influx token | `dashboard.env`: `INFLUX_DASHBOARD_TOKEN` | InfluxDB administrator | Yes | Configured InfluxDB HTTPS endpoint | Read all three buckets only | Validate scope and load dashboard data | Replace, test this service, then revoke old token |
| Syslog destination | `config.yaml`: `observability.syslog.*` (no credential variable) | Your log-platform administrator | Hostname is not a secret; CA/trust material may be sensitive | Configured UDP/TCP/TLS host and port | Receive only fixed operational event codes | Induce a non-sensitive test event and confirm receipt | Follow the log platform's certificate/trust rotation process |

The `validate` command checks configuration loading and InfluxDB health. It does **not** prove
provider authentication or that a token has every required bucket grant. The one-shot and record
checks later in this guide provide that evidence.

## 1. Obtain Sigenergy monitoring credentials

Use the official [Sigenergy developer portal](https://developer.sigencloud.com/). Sigenergy's
portal documentation currently describes third-party developer access as invitation-based; request
an invitation through Sigenergy if registration requires a code. Existing owners/installers may be
able to sign in with their existing Sigenergy account. Approval may take several business days.

1. Sign in or register, supplying the requested application-use details.
2. Create an application and select **Monitoring** as its purpose.
3. Do not request **Control** and do not enable VPP dispatch. This project has no control path.
4. After approval, open the application's settings and copy its AppKey.
5. Generate the AppSecret. The portal displays it only once, so put it directly in the password
   manager. If it is lost, generate a replacement rather than trying to recover it from logs.
6. In the application's authentication/system view, authorize the property and obtain the system
   identifier used by the `/openapi/systems/{systemId}/...` endpoints. Portal labels can change; if
   the ID is not shown, use the authorized-system API/documentation listed for the application or
   ask Sigenergy support. Do not substitute a device serial number.
7. Check the application's API list and note the published call limits. Keep the supplied pacing
   defaults at or below those limits.

Choose the configuration `region` that maps to the property's official Sigenergy OpenAPI host:

| `region` | Host used by this release |
|---|---|
| `eu` | `https://openapi-eu.sigencloud.com` |
| `ap` | `https://openapi-apac.sigencloud.com` |
| `mea` | `https://openapi-eu.sigencloud.com` |
| `cn` | `https://openapi-cn.sigencloud.com` |
| `anz` | `https://openapi-aus.sigencloud.com` |
| `la` | `https://openapi-us.sigencloud.com` |
| `na` | `https://openapi-us.sigencloud.com` |
| `jp` | `https://openapi-jp.sigencloud.com` |

Store the three values only in the telemetry service's environment file. For a second property,
use a second non-identifying suffix and matching placeholders in `config.yaml`, for example
`SIGENERGY_HOME_02_APP_KEY`, `SIGENERGY_HOME_02_APP_SECRET`, and
`SIGENERGY_HOME_02_SYSTEM_ID`. All properties handled by this deployment share the telemetry
service identity, so use a separate deployment if operators require stronger per-property secret
isolation.

## 2. Obtain Octopus tariff values

Version 0.1 calls Octopus's public `standard-unit-rates` REST endpoint and does not use an Octopus
API key. Find the exact product and electricity tariff codes shown for the property in the Octopus
account/contract, or inspect the matching product through the official
[Octopus REST API](https://docs.octopus.energy/rest/guides/api-basics/). Copy the full strings
exactly; do not infer a regional suffix.

An Octopus account API key can be generated under **Developer settings** in the account dashboard,
as described in [Octopus's API access guidance](https://octopus.energy/help-and-faqs/articles/how-do-i-access-the-octopus-api/).
Keep it in a password manager, but do not add it to any version 0.1 environment file. Authenticated
GraphQL support for Intelligent Octopus bonus dispatches (`flexPlannedDispatches`) is planned, not
implemented.

> [!WARNING]
> The current adapter rejects any standard-rate interval longer than two hours. Some real
> Intelligent Octopus tariff responses contain 6-hour or 18-hour intervals, so the illustrative
> Intelligent codes in `config.example.yaml` can fail with `tariff provider returned an invalid
> interval`. Do not treat that example as production-compatible. Test the property's exact tariff
> with `tariff --once`; this limitation must be resolved before using an affected tariff.

## 3. Confirm Open-Meteo usage

The current forecast adapter uses `https://api.open-meteo.com/v1/forecast`. According to the
[Open-Meteo forecast documentation](https://open-meteo.com/en/docs), the public non-commercial API
does not require an API key. Commercial reserved resources use a customer endpoint and key, which
this release cannot configure. Review Open-Meteo's current usage terms and expected request volume
before production use; do not put a commercial key into an unrelated environment variable.

Each configured roof plane causes a separate forecast request. The shared HTTP pacing settings,
worker schedule, and provider-published limits therefore need to cover the total number of roof
planes across all properties.

## 4. Prepare InfluxDB OSS 2.x

This application targets InfluxDB OSS 2.x. The `influx version` command reports the local CLI
version, not the server version. The unauthenticated server health response reports the actual
server version; confirm that its JSON `version` value starts with `2.` before creating resources.

**Run on an administrator workstation with the Influx CLI configured; the first command is client-only and the second queries the server:**

```bash
set -euo pipefail
influx version
curl --fail --silent --show-error https://influxdb.example.invalid:8086/health
```

Create three distinct buckets. Select retention periods that match your privacy and forecasting
needs; reconciliation needs enough telemetry and planning history to learn useful factors. The
official references are [Create a bucket](https://docs.influxdata.com/influxdb/v2/admin/buckets/create-bucket/)
and [Create a custom API token](https://docs.influxdata.com/influxdb/v2/admin/tokens/create-token/).

`config.yaml` takes the organization **name** and the three bucket **names**. The authorization
commands below instead take immutable organization and bucket **IDs**. Obtain them from the
administrator CLI; do not paste an ID into a name field or infer an ID from a name.

**Run on the InfluxDB administrator workstation; replace the organization name, then record the returned organization ID:**

```bash
set -euo pipefail
influx org list --name ORG_NAME
```

**Run on the InfluxDB administrator workstation; replace every uppercase ID/duration placeholder first:**

```bash
set -euo pipefail
influx bucket create --org-id ORG_ID --name solar_telemetry --retention RETENTION_DURATION
influx bucket create --org-id ORG_ID --name solar_tariff --retention RETENTION_DURATION
influx bucket create --org-id ORG_ID --name solar_planning --retention RETENTION_DURATION
influx bucket list --org-id ORG_ID
```

Record the three bucket IDs from the list. Keep `ORG_NAME`, `solar_telemetry`, `solar_tariff`, and
`solar_planning` as names in `config.yaml`; use only the recorded IDs in the CLI commands. Create
five different custom tokens with the exact
grants below. Only an Influx identity allowed to create authorizations can do this. Token values are
shown when created and may not be retrievable later, so run this in a private terminal and capture
each result directly into the password manager. Never use an operator/all-access token in a
service environment.

| Token | Read grants | Write grants |
|---|---|---|
| telemetry | None | `solar_telemetry` |
| tariff | None | `solar_tariff` |
| forecast-plan | `solar_telemetry`, `solar_tariff`, `solar_planning` | `solar_planning` |
| reconciliation | `solar_telemetry`, `solar_planning` | `solar_planning` |
| dashboard | `solar_telemetry`, `solar_tariff`, `solar_planning` | None |

**Run on the private InfluxDB administrator workstation; replace IDs before each command and store each returned token immediately:**

```bash
set -euo pipefail
influx auth create --org-id ORG_ID --description solar-telemetry \
  --write-bucket TELEMETRY_BUCKET_ID
influx auth create --org-id ORG_ID --description solar-tariff \
  --write-bucket TARIFF_BUCKET_ID
influx auth create --org-id ORG_ID --description solar-forecast-plan \
  --read-bucket TELEMETRY_BUCKET_ID --read-bucket TARIFF_BUCKET_ID \
  --read-bucket PLANNING_BUCKET_ID --write-bucket PLANNING_BUCKET_ID
influx auth create --org-id ORG_ID --description solar-reconciliation \
  --read-bucket TELEMETRY_BUCKET_ID --read-bucket PLANNING_BUCKET_ID \
  --write-bucket PLANNING_BUCKET_ID
influx auth create --org-id ORG_ID --description solar-dashboard \
  --read-bucket TELEMETRY_BUCKET_ID --read-bucket TARIFF_BUCKET_ID \
  --read-bucket PLANNING_BUCKET_ID
```

Use HTTPS whenever InfluxDB traffic crosses a network boundary. Trust the private CA in the LXC's
system trust store; never disable certificate verification.

## 5. Create the Proxmox LXC

The unverified minimum baseline is one vCPU, 512 MB RAM, and 4 GB disk. It is derived from the
service limits but has not completed a live LXC soak. Use the live-tested/recommended baseline of
one vCPU, 1 GB RAM, and 8 GB disk for operational headroom and for the co-located Nginx proxy
described below. Increase disk
space if the configured outbox limits plus OS logs and upgrades cannot fit above the filesystem
reserve.

In the Proxmox web interface:

1. Download a Debian 12 standard container template from the node's local storage.
2. Select **Create CT**, choose a unique CT ID and non-identifying hostname, and leave
   **Unprivileged container** enabled.
3. Select the Debian 12 template, the recommended resources above, and a fixed DHCP reservation or
   static address on a trusted service network.
4. Configure working DNS and NTP. Do not expose the container directly to the internet.
5. Start without extra features. The application itself does not need Docker or nesting. If
   systemd fails because of the host's namespace policy, enable only `nesting=1`, retest, and keep
   the container unprivileged; do not enable a privileged container as a workaround.
6. Start the LXC, update Debian, and verify that its clock and DNS are correct.

The official [`pct` command reference](https://pve.proxmox.com/pve-docs/pct.1.html) provides the
equivalent command-line management operations.

**Run inside the new LXC as root:**

```bash
set -euo pipefail
apt-get update
apt-get upgrade
apt-get install ca-certificates curl python3 python3-venv util-linux
timedatectl status
getent hosts api.open-meteo.com api.octopus.energy
```

## 6. Install a verified release

Follow every step in [`deployment/LXC.md`](../deployment/LXC.md#release-inputs): download and
verify release attestations on a trusted administrator workstation, transfer only verified
artifacts, install the wheel without a privileged build, create the five Unix identities and
private environment files, and install the supplied systemd units.

Do not deploy a moving branch, unverified archive, or package resolved from the internet as root.
Do not start services until configuration, file ownership, network policy, and scoped validation
are complete.

## 7. Configure properties and service environments

Edit `/etc/solar-battery-forecaster/config.yaml` as root. For every property:

- Replace `id` with a non-identifying unique alias.
- Set the IANA time zone and the property's latitude/longitude. Treat the YAML file as private
  because coordinates can identify a home.
- Describe every roof plane independently: panel count, panel watts, tilt and compass azimuth.
- Set inverter rated power and the exact Sigenergy region.
- Set the battery's usable capacity, minimum/maximum state of charge, reserve, maximum charge
  power and efficiency from its verified specification/configuration.
- Set the exact Octopus product/tariff codes and a reviewed cheap-rate threshold.
- Review schedules and HTTP pacing against provider limits and the number of properties/arrays.

The repository's sample 6 kW inverter, 9 kWh battery, coordinates, identifiers and tariff codes are
fictional/illustrative. Copy the file and replace them; never commit the real copy.

### Complete configuration reference

All accepted scalar fields in `config.example.yaml` are listed below. Ranges are enforced at
configuration load unless the text says "operational rule". Bytes are integer bytes; seconds,
minutes, hours, watts, kilowatts (kW), kilowatt-hours (kWh), and percentages use the units named in
the field. Safe examples are fictional and must be replaced or reviewed. HTTP, outbox and schedule
scalars are non-secret operational settings; their authoritative sources are the enforced
implementation bounds plus the provider limits, outage objective, capacity evidence or operator
policy named in each row. The configuration as a whole remains private because other sections hold
property location and infrastructure metadata.

| Influx field | Units/range or enum | Privacy and authoritative source | Safe example |
|---|---|---|---|
| `influxdb.url` | Required URL; HTTPS is an operational rule across a network boundary | Private infrastructure; Influx administrator | `https://influxdb.example.invalid:8086` |
| `influxdb.org` | Required organization **name**, not ID | Private tenant metadata; `influx org list`/administrator | `example-org` |
| `telemetry_bucket` | Non-empty name; all three bucket names must differ | Private infrastructure; Influx administrator | `solar_telemetry` |
| `tariff_bucket` | Non-empty, distinct bucket name | Private infrastructure; Influx administrator | `solar_tariff` |
| `planning_bucket` | Non-empty, distinct bucket name | Private infrastructure; Influx administrator | `solar_planning` |
| `tokens.telemetry` | Fixed environment placeholder | Secret; matching custom authorization | `${INFLUX_TELEMETRY_TOKEN}` |
| `tokens.tariff` | Fixed environment placeholder | Secret; matching custom authorization | `${INFLUX_TARIFF_TOKEN}` |
| `tokens.forecast-plan` | Fixed environment placeholder | Secret; matching custom authorization | `${INFLUX_FORECAST_PLAN_TOKEN}` |
| `tokens.reconciliation` | Fixed environment placeholder | Secret; matching custom authorization | `${INFLUX_RECONCILIATION_TOKEN}` |
| `tokens.dashboard` | Fixed environment placeholder | Secret; matching read-only authorization | `${INFLUX_DASHBOARD_TOKEN}` |

| HTTP field | Units/range | Purpose/source | Safe example |
|---|---|---|---|
| `minimum_spacing_seconds` | 0–60 seconds | Minimum serialized provider-call spacing; use the strictest provider limit divided across configured work | `0.5` |
| `max_response_bytes` | 16,384–8,388,608 bytes | Maximum decompressed provider JSON; raise only for a measured, reviewed need | `1048576` |
| `max_attempts` | 1–10 attempts | Total bounded HTTP attempts | `3` |
| `retry_base_seconds` | 0.1–60 seconds | First ordinary backoff delay | `1` |
| `retry_max_seconds` | 1–300 seconds | Maximum ordinary backoff delay; operationally keep at least the base | `30` |
| `retry_after_max_seconds` | 1–3,600 seconds | Longest provider `Retry-After` handled inline; longer values defer the next cycle | `300` |
| `jitter_seconds` | 0–10 seconds | Random addition that reduces synchronized requests | `0.5` |

| Outbox field | Units/range | Purpose/source | Safe example |
|---|---|---|---|
| `state_directory` | Absolute, non-root path; required for writer scopes | Private local state; fixed per service environment, not shared YAML | `${OUTBOX_STATE_DIRECTORY}` |
| `database_max_bytes` | 1,048,576–2,147,483,648 bytes | Per-writer SQLite maximum; size from outage objective/free disk | `134217728` |
| `max_record_bytes` | 16,384–16,777,216 bytes | Maximum one serialized Influx batch | `2097152` |
| `max_records` | 100–10,000,000 records | Maximum queued records; no silent eviction | `100000` |
| `filesystem_min_free_bytes` | 16,777,216–17,179,869,184 bytes | Free-space floor below which collection stops | `268435456` |
| `journal_headroom_bytes` | 1,048,576–268,435,456 bytes | Reserved SQLite WAL/journal space | `8388608` |
| `collection_reserve_bytes` | 16,384–16,777,216 bytes; must cover `max_record_bytes` | Space admitted before another provider collection | `2097152` |
| `drain_max_records` | 1–1,000 records | Maximum records per replay batch | `32` |
| `drain_max_bytes` | 16,384–67,108,864 bytes; must cover `max_record_bytes` | Maximum bytes per replay batch | `8388608` |
| `retry_base_seconds` | 1–300 seconds | First database-delivery retry delay | `5` |
| `retry_max_seconds` | 1–3,600 seconds; at least base | Maximum database-delivery retry delay | `300` |

`collection_reserve_bytes + journal_headroom_bytes` must not exceed `database_max_bytes`. Ensure the
LXC disk can hold four maximum outboxes, their WAL/headroom, journald, packages, and the configured
free-space floors; the bounds are safety rails, not a capacity recommendation.

| Observability/syslog field | Units/range or enum | Privacy and authoritative source | Safe example |
|---|---|---|---|
| `status_directory` | Absolute, non-root path | Fixed per service environment; runtime-only sanitized projection | `${STATUS_DIRECTORY}` |
| `heartbeat_seconds` | 10–300 seconds | Status write frequency; operator monitoring objective | `30` |
| `stale_after_seconds` | 30–900 seconds | Dashboard stale threshold; set above heartbeat plus scheduling jitter | `90` |
| `syslog.enabled` | `true`/`false` | Whether fixed events are forwarded; log administrator | `false` |
| `syslog.host` | DNS name/IP, ≤253 characters, no whitespace, slash or backslash; required when enabled | Infrastructure metadata; exact TLS certificate name from log administrator | `syslog.example.invalid` |
| `syslog.port` | 1–65,535 | Destination listener from log administrator | `6514` |
| `syslog.transport` | `udp`, `tcp`, or `tls` | Listener protocol; use TLS outside a trusted LAN | `tls` |
| `syslog.connect_timeout_seconds` | 0.1–30 seconds | Bounded connection timeout | `3` |
| `syslog.queue_size` | 10–5,000 events | In-memory best-effort queue; size from outage/traffic objective | `256` |

| Schedule field | Units/range | Meaning/source | Safe example |
|---|---|---|---|
| `telemetry_seconds` | ≥300 seconds | Collection interval; Sigenergy limits and desired resolution | `300` |
| `telemetry_stale_after_seconds` | ≥300 seconds | Maximum telemetry age accepted by planning | `900` |
| `tariff_minutes` | ≥30 minutes | Tariff refresh interval; Octopus limits/price publication | `360` |
| `forecast_hour` | 0–23 local hour | Time after which tomorrow's plan becomes due | `21` |
| `forecast_minute` | 0–59 local minute | Minute component of plan time | `30` |
| `reconciliation_hour` | 0–23 local hour | Local daily reconciliation hour | `0` |
| `reconciliation_minute` | 0–59 local minute | Minute component of reconciliation time | `15` |
| `worker_scan_seconds` | ≥10 seconds | Due-job scan interval | `60` |
| `property_phase_seconds` | 0–60 seconds | Delay between properties to avoid request bursts | `2` |
| `reconciliation_catch_up_days` | 1–30 days | Missing past local days checked after downtime | `7` |

| Property/array field | Units/range or enum | Privacy and authoritative source | Safe example |
|---|---|---|---|
| `id` | Unique lowercase alias matching `[a-z0-9][a-z0-9_-]+` | Non-identifying operator alias; never address/account/serial | `home-01` |
| `timezone` | IANA time-zone name (operationally validated when used) | Property locale; IANA/operator | `Europe/London` |
| `latitude` | −90–90 degrees | Sensitive location; verified mapping/site survey | `0.0` (non-property placeholder) |
| `longitude` | −180–180 degrees | Sensitive location; verified mapping/site survey | `0.0` (non-property placeholder) |
| `arrays[].name` | Required text | Non-sensitive descriptive alias; installation plan | `south-roof` |
| `arrays[].panel_count` | Integer >0 | Installation plan/inverter portal | `10` |
| `arrays[].panel_power_w` | >0 watts per panel | Panel datasheet | `440` |
| `arrays[].tilt_degrees` | 0–90 degrees from horizontal | Roof survey/installer drawing | `35` |
| `arrays[].azimuth_degrees` | 0≤value<360; north 0°, east 90°, south 180°, west 270° | Roof survey/verified map | `180` |
| `arrays[].performance_ratio` | >0–1 | Commissioning/history estimate; begin conservatively | `0.84` |

| Equipment/planning field | Units/range or enum | Privacy and authoritative source | Safe example |
|---|---|---|---|
| `inverter.adapter` | Current enum in practice: `sigenergy_cloud` | Adapter selected by installed release | `sigenergy_cloud` |
| `inverter.rated_power_kw` | >0 kW | Inverter datasheet/commissioning record | `6.0` |
| `inverter.region` | `eu`, `ap`, `mea`, `cn`, `anz`, `la`, `na`, or `jp` | Sigenergy property region/portal | `eu` |
| `inverter.app_key` | Environment placeholder | Secret AppKey from portal | `${SIGENERGY_HOME_APP_KEY}` |
| `inverter.app_secret` | Environment placeholder | Secret one-time AppSecret from portal | `${SIGENERGY_HOME_APP_SECRET}` |
| `inverter.system_id` | Environment placeholder | Sensitive authorized system ID, not serial | `${SIGENERGY_HOME_SYSTEM_ID}` |
| `battery.usable_capacity_kwh` | >0 kWh | Battery datasheet/current commissioning configuration | `9.0` |
| `battery.minimum_soc_percent` | 0–100%, strictly below maximum | Installer/manufacturer reserve policy | `10` |
| `battery.maximum_soc_percent` | 0–100%, strictly above minimum | Installer/manufacturer operating policy | `100` |
| `battery.reserve_kwh` | ≥0 kWh; operationally no more than usable capacity | Owner resilience policy | `1.0` |
| `battery.max_charge_power_kw` | >0 kW | Inverter/battery/grid constraint | `6.0` |
| `battery.charge_efficiency` | >0–1 | Datasheet or measured commissioning value | `0.94` |
| `forecast.adapter` | Current enum in practice: `open_meteo` | Adapter selected by installed release | `open_meteo` |
| `forecast.initial_correction_factor` | 0.25–2 | Start at neutral until daily history exists | `1.0` |
| `forecast.conservative_multiplier` | >0–1 | Owner risk tolerance; lower is more conservative | `0.80` |
| `load.expected_kwh_until_next_cheap_window` | ≥0 kWh | Bill/meter history until learned load exists | `8.0` |
| `tariff.adapter` | Current enum in practice: `octopus` | Adapter selected by installed release | `octopus` |
| `tariff.product_code` | Required exact text | Property's Octopus account/public product API | `EXAMPLE-PRODUCT` |
| `tariff.tariff_code` | Required exact text | Property's Octopus account/public product API | `E-1R-EXAMPLE-A` |
| `tariff.cheap_rate_threshold_pence` | ≥−100 pence/kWh including VAT | Owner's charging policy/current contract | `10.0` |
| `tariff.stale_after_minutes` | 30–2,880 minutes | Maximum tariff age allowed by planning | `480` |

The YAML objects `influxdb`, `http`, `outbox`, `observability`, `schedule`, `inverter`, `battery`,
`forecast`, `load`, `tariff`, and `syslog` are structural mappings rather than scalar settings.
`properties` and each property's `arrays` are repeated lists: add one complete property object per
site and one array object per distinct roof plane. The five keys under `tokens` and the service
environment filenames are fixed process scopes, not user-defined names. Array kWp, issued snapshot
IDs, corrected forecasts, status fields, measurements and learned factors are derived/runtime
values and must not be added to `config.yaml`. Unknown fields are rejected in security-sensitive
global sections; adapter-specific models may also reject unsupported adapter names at runtime.

Put secrets in only the matching root-owned mode-0640 environment file:

| File | Values |
|---|---|
| `telemetry.env` | Telemetry Influx token and all configured Sigenergy AppKey/AppSecret/system-ID aliases |
| `tariff.env` | Tariff Influx token; no Octopus credential in version 0.1 |
| `forecast-plan.env` | Forecast-plan Influx token; no Open-Meteo credential in version 0.1 |
| `reconciliation.env` | Reconciliation Influx token |
| `dashboard.env` | Dashboard read-only Influx token |

The full ownership and cross-user readability checks are in
[`deployment/LXC.md`](../deployment/LXC.md#install-without-a-privileged-build). Run them before
starting a service, and never print an environment file to a log or terminal capture.

## 8. Enforce network boundaries

Allow outbound connections only to destinations required by the enabled configuration:

| Direction | Destination | Purpose |
|---|---|---|
| Outbound TCP 443 | The one selected Sigenergy regional host | Telemetry authentication and reads |
| Outbound TCP 443 | `api.octopus.energy` | Public tariff rates |
| Outbound TCP 443 | `api.open-meteo.com` | Tilted irradiance forecast |
| Outbound TCP 8086 or configured HTTPS port | Exact InfluxDB endpoint | Health, reads and writes |
| Outbound selected protocol/port | Exact syslog destination, only if enabled | Fixed operational events |
| Outbound DNS/NTP | Approved local resolvers/time source | Name resolution and time |
| Inbound TCP 443 | Trusted LAN/VPN clients to Nginx only | Authenticated dashboard |

Package repositories and the administrative transfer path may be temporarily needed during
provisioning/upgrades. Remove temporary broad egress afterwards. Proxmox firewall rules are
IP-based, while provider addresses can change; use an approved filtering proxy or a controlled
process that resolves and reviews current provider addresses instead of permanently allowing all
outbound HTTPS.

## 9. Enable syslog if required

Journald remains the local source for application log messages. Optional remote syslog forwards
only bounded, fixed operational event codes; it does not forward arbitrary exception text,
credentials, property IDs, coordinates, provider payloads, or SQLite contents.

In `config.yaml`, set `observability.syslog.enabled: true`, the exact host and port, and one of
`udp`, `tcp`, or `tls`. Prefer certificate-verified `tls` outside a trusted LAN and install the
issuing CA in the LXC trust store. TLS validates both the certificate chain and the configured
`host` as the server name, so configure the DNS name present in the certificate rather than an IP
address unless the certificate has that IP in its subject alternative names. Version 0.1 does not
load a client certificate and does not support mutual TLS (mTLS); place an approved relay in front
of an mTLS-only collector. Permit only that destination through the firewall. Collection
continues if syslog is slow or unavailable; queue loss/failure appears in the status view.

## 10. Validate and start all five services

First run scoped configuration/Influx health validation under each service identity. This proves
that the relevant environment can load and the Influx endpoint responds, but not provider access
or exact bucket grants. The checked-in maintenance templates use systemd `EnvironmentFile=` and
execute the application directly as `solar-SCOPE`; they never interpret a secret file as shell
code. They are not enabled at boot.

**Run inside the LXC as root:**

```bash
set -euo pipefail
for scope in telemetry tariff forecast-plan reconciliation dashboard; do
  systemctl start "solar-battery-validate@$scope.service"
  systemctl show "solar-battery-validate@$scope.service" \
    --property=Result --property=ExecMainStatus --no-pager
done
```

Every instance must show `Result=success` and `ExecMainStatus=0`. Never source an environment file;
systemd environment-file syntax is data syntax and values may contain shell metacharacters.

Run live one-shot telemetry and tariff collection. These calls contact the real providers and can
write to InfluxDB or, on write failure, to the fallback outbox. Stop the corresponding continuous
unit first so two processes never operate on the same outbox.

**Run inside the LXC as root during the acceptance window:**

```bash
set -euo pipefail
systemctl stop solar-battery-telemetry solar-battery-tariff
systemctl start solar-battery-once@telemetry.service
systemctl start solar-battery-once@tariff.service
systemctl show solar-battery-once@telemetry.service solar-battery-once@tariff.service \
  --property=Result --property=ExecMainStatus --no-pager
```

Both instances must show `Result=success` and `ExecMainStatus=0`.

Confirm new, plausible, correctly timestamped `energy_telemetry` and `electricity_tariff` records
in InfluxDB. A zero exit alone is insufficient. Forecast planning is schedule-aware and normally
targets tomorrow only after the configured local forecast time; reconciliation is also due-time
aware. Run/observe those processes at their configured due times, then confirm a complete
`pv_forecast`, a `battery_decision`, and later a `pv_daily` record. Review the displayed values
against the physical system and provider portals.

Enable all five independent services only after the one-shot checks succeed.

**Run inside the LXC as root:**

```bash
set -euo pipefail
systemctl enable --now solar-battery-telemetry
systemctl enable --now solar-battery-tariff
systemctl enable --now solar-battery-forecast-plan
systemctl enable --now solar-battery-reconciliation
systemctl enable --now solar-battery-dashboard
systemctl --no-pager --full status \
  solar-battery-telemetry solar-battery-tariff solar-battery-forecast-plan \
  solar-battery-reconciliation solar-battery-dashboard
```

### Runtime and isolation acceptance

Prove all units are enabled/active, no unit has a failed result or unexpected restart, and current
memory remains below its enforced maximum. Capture the values after at least one normal collection
cycle; `NRestarts=0` is expected for a clean acceptance run. `MemoryCurrent` may be `[not set]`
briefly during activation, so repeat after the process is active.

**Run inside the LXC as root:**

```bash
set -euo pipefail
for unit in telemetry tariff forecast-plan reconciliation dashboard; do
  service="solar-battery-$unit.service"
  systemctl is-enabled --quiet "$service"
  systemctl is-active --quiet "$service"
  test "$(systemctl show "$service" --property=Result --value)" = success
  test "$(systemctl show "$service" --property=NRestarts --value)" = 0
  current="$(systemctl show "$service" --property=MemoryCurrent --value)"
  maximum="$(systemctl show "$service" --property=MemoryMax --value)"
  test "$current" -le "$maximum"
done
test -z "$(systemctl --failed --no-legend 'solar-battery-*')"
```

The block must exit zero. Now stop and restart each process individually and prove every peer stays
active. This tests failure isolation; it does not simulate a crash or alter provider/network state.

**Run inside the LXC as root:**

```bash
set -euo pipefail
for target in telemetry tariff forecast-plan reconciliation dashboard; do
  systemctl stop "solar-battery-$target.service"
  test "$(systemctl is-active "solar-battery-$target.service")" = inactive
  for peer in telemetry tariff forecast-plan reconciliation dashboard; do
    if test "$peer" != "$target"; then
      systemctl is-active --quiet "solar-battery-$peer.service"
    fi
  done
  systemctl start "solar-battery-$target.service"
  systemctl is-active --quiet "solar-battery-$target.service"
done
```

The block must exit zero; all five services must end active.

In an explicitly authorized interruption window, prove `Restart=on-failure` with one controlled
SIGKILL. The example targets telemetry; it records the counter first, checks every peer immediately
after the signal, and polls for at most 25 seconds (the unit has a 15-second restart delay). No
provider, database, network or peer service is deliberately disrupted.

**Run inside the LXC as root during an authorized automatic-restart test:**

```bash
set -euo pipefail
target=telemetry
service="solar-battery-$target.service"
before="$(systemctl show "$service" --property=NRestarts --value)"
systemctl is-active --quiet "$service"
systemctl kill --kill-whom=main --signal=SIGKILL "$service"
for peer in tariff forecast-plan reconciliation dashboard; do
  systemctl is-active --quiet "solar-battery-$peer.service"
done
recovered=false
for attempt in {1..50}; do
  after="$(systemctl show "$service" --property=NRestarts --value)"
  if systemctl is-active --quiet "$service" && test "$after" -gt "$before"; then
    recovered=true
    break
  fi
  sleep 0.5
done
test "$recovered" = true
for peer in tariff forecast-plan reconciliation dashboard; do
  systemctl is-active --quiet "solar-battery-$peer.service"
done
```

The block must exit zero: telemetry is active again, its `NRestarts` value is greater than the
captured value, and all four peers remained active. Record the before/after counters. A failure or
timeout is a production blocker; inspect the local journal before continuing.

### Outbox permissions and healthy baseline

Stop one writer before using its outbox maintenance unit. Repeat for all four writers. The
permission checks themselves are silent and the block must exit zero. It then deliberately prints
one sanitized status JSON object per writer; each object must show zero pending, blocked and
quarantined records before the controlled outage test.

**Run inside the LXC as root:**

```bash
set -euo pipefail
for worker in telemetry tariff forecast-plan reconciliation; do
  systemctl stop "solar-battery-$worker.service"
  state="/var/lib/solar-battery-$worker"
  test "$(stat -c '%U:%G %a' "$state")" = "solar-$worker:solar-$worker 700"
  test -z "$(find "$state" -maxdepth 1 -type f -perm /077 -print -quit)"
  systemctl start "solar-battery-outbox-status@$worker.service"
  journalctl -u "solar-battery-outbox-status@$worker.service" -o cat --no-pager \
    | grep '^{' | tail -n 1
  systemctl start "solar-battery-$worker.service"
done
```

### Controlled InfluxDB fallback and replay acceptance

Run this only in an authorized acceptance window. It does **not** stop, firewall, or reconfigure the
shared InfluxDB server. It stops only this deployment's telemetry worker and gives a non-enabled
one-shot unit a temporary private configuration whose Influx URL is the local discard port and
whose property list contains exactly one explicitly selected non-identifying alias. The main
configuration is read but never modified. Start only with a healthy telemetry outbox showing zero
pending records. Record a narrow UTC test window and the count of distinct telemetry timestamps in
it using an authorized Influx administrator CLI.

**Run on the InfluxDB administrator workstation; replace the organization, property alias, and absolute UTC window:**

```bash
set -euo pipefail
influx query --org ORG_NAME '
from(bucket: "solar_telemetry")
  |> range(start: TEST_START_UTC, stop: TEST_STOP_UTC)
  |> filter(fn: (r) => r._measurement == "energy_telemetry" and r.property == "home-01")
  |> keep(columns: ["_time"])
  |> unique(column: "_time")
  |> count(column: "_time")'
```

Record the baseline count, then use the installed virtual environment's PyYAML parser to select
exactly one property and create a new root-owned temporary file with an unreachable Influx URL.
The quoted Python here-document does not evaluate the YAML or environment placeholders as shell
code. Replace `home-01` with the same non-identifying alias used in the query. The final output must
be `selected property home-01; Influx URL http://127.0.0.1:9` (with the chosen alias) followed by
`root:solar-config 640`.

**Run inside the LXC as root:**

```bash
set -euo pipefail
property_alias=home-01
systemctl stop solar-battery-telemetry.service
/opt/solar-battery-forecaster/.venv/bin/python - "$property_alias" <<'PY'
import grp
import os
import sys
from pathlib import Path

import yaml

source = Path("/etc/solar-battery-forecaster/config.yaml")
destination = Path("/etc/solar-battery-forecaster/acceptance-outage.yaml")
alias = sys.argv[1]
config = yaml.safe_load(source.read_text(encoding="utf-8"))
matches = [item for item in config["properties"] if item.get("id") == alias]
if len(matches) != 1:
    raise SystemExit("selected property alias must match exactly once")
config["properties"] = matches
config["influxdb"]["url"] = "http://127.0.0.1:9"
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
descriptor = os.open(destination, flags, 0o640)
os.fchown(descriptor, 0, grp.getgrnam("solar-config").gr_gid)
os.fchmod(descriptor, 0o640)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    yaml.safe_dump(config, output, sort_keys=False)
written = yaml.safe_load(destination.read_text(encoding="utf-8"))
if len(written["properties"]) != 1 or written["properties"][0]["id"] != alias:
    raise SystemExit("temporary configuration property selection failed")
if written["influxdb"]["url"] != "http://127.0.0.1:9":
    raise SystemExit("temporary configuration Influx URL is not isolated")
print(f"selected property {alias}; Influx URL {written['influxdb']['url']}")
PY
cleanup_acceptance_config() {
  rm -- /etc/solar-battery-forecaster/acceptance-outage.yaml
}
trap cleanup_acceptance_config EXIT
test "$(stat -c '%U:%G %a' /etc/solar-battery-forecaster/acceptance-outage.yaml)" = \
  'root:solar-config 640'
stat -c '%U:%G %a' /etc/solar-battery-forecaster/acceptance-outage.yaml
if systemctl start solar-battery-outage-test@telemetry.service; then
  echo 'unexpected direct-write success' >&2
  exit 1
fi
systemctl show solar-battery-outage-test@telemetry.service \
  --property=Result --property=ExecMainStatus --no-pager
systemctl start solar-battery-outbox-status@telemetry.service
journalctl -u solar-battery-outbox-status@telemetry.service -o cat --no-pager \
  | grep '^{' | tail -n 1
```

The outage unit must report `Result=exit-code` and `ExecMainStatus=1`: the selected property's
provider collection succeeded but the intentionally unreachable Influx destination left one
undelivered record. Because the temporary YAML contains exactly one property, status must show
pending records increased from zero to one, with no quarantine or blocked stream. The block's EXIT
trap securely removes the exact temporary file on success or error. Drain using the real
configuration while the continuous writer remains stopped, and check the empty status.

**Run inside the LXC as root:**

```bash
set -euo pipefail
systemctl reset-failed solar-battery-outage-test@telemetry.service
systemctl start solar-battery-outbox-drain@telemetry.service
journalctl -u solar-battery-outbox-drain@telemetry.service -o cat --no-pager \
  | grep '^delivered ' | tail -n 1
systemctl start solar-battery-outbox-status@telemetry.service
journalctl -u solar-battery-outbox-status@telemetry.service -o cat --no-pager \
  | grep '^{' | tail -n 1
systemctl start solar-battery-telemetry.service
```

Drain output must say `delivered 1 record(s)` and status must return to zero pending records. Repeat
the earlier administrator query: the count must have increased by exactly one, demonstrating one
confirmed Influx timestamp after replay. If any prerequisite or expected result differs, stop the
test and investigate; do not delete, retry, or mutate the outbox manually.

### Local dashboard status freshness

The local API must return all five fixed service names, a current generated time, non-stale
heartbeats, and a last cycle result for each process after one normal cycle.

**Run inside the LXC as root after all services have completed a cycle:**

```bash
set -euo pipefail
curl --fail --silent http://127.0.0.1:8088/api/status | python3 -c '
import datetime, json, sys
payload = json.load(sys.stdin)
expected = {"telemetry", "tariff", "forecast-plan", "reconciliation", "dashboard"}
services = payload["services"]
assert {item["service"] for item in services} == expected
assert all(item["stale"] is False for item in services)
assert all(item["lifecycle"] == "running" for item in services)
workers = [item for item in services if item["service"] != "dashboard"]
assert all(item["last_cycle_result"] in {"success", "running"} for item in workers)
generated = datetime.datetime.fromisoformat(payload["generated_at"])
now = datetime.datetime.now(datetime.timezone.utc)
assert abs((now - generated).total_seconds()) < 30
'
```

The command must exit zero and print nothing. A dashboard heartbeat can be current before a
schedule-aware forecast/reconciliation job becomes due; the earlier record acceptance remains a
separate requirement.

## 11. Publish the dashboard safely

The dashboard service listens on `127.0.0.1:8088` and has no built-in authentication. Keep it on
loopback. The simplest supported topology is an authenticated HTTPS Nginx proxy in the same LXC;
the 1 GB/8 GB recommendation includes headroom for it. If the proxy is elsewhere, connect it using
a protected tunnel/VPN whose LXC endpoint still terminates on loopback. Never change the service to
`0.0.0.0` merely to make a remote proxy work.

Obtain a certificate for a private DNS name using your organization's approved PKI or certificate
process. The following example assumes the certificate and private key already exist and trusted
LAN/VPN firewall rules are in place.

**Run inside the LXC as root:**

```bash
set -euo pipefail
apt-get install nginx apache2-utils
htpasswd -c /etc/nginx/solar-dashboard.htpasswd DASHBOARD_USERNAME
chown root:www-data /etc/nginx/solar-dashboard.htpasswd
chmod 0640 /etc/nginx/solar-dashboard.htpasswd
```

**Save as `/etc/nginx/sites-available/solar-dashboard` inside the LXC; replace every uppercase placeholder:**

```nginx
limit_req_zone $binary_remote_addr zone=solar_dashboard:10m rate=5r/s;

server {
    listen 443 ssl;
    server_name DASHBOARD_DNS_NAME;

    ssl_certificate /etc/nginx/tls/DASHBOARD_CERTIFICATE.pem;
    ssl_certificate_key /etc/nginx/tls/DASHBOARD_PRIVATE_KEY.pem;

    auth_basic "Solar dashboard";
    auth_basic_user_file /etc/nginx/solar-dashboard.htpasswd;

    location / {
        limit_req zone=solar_dashboard burst=20 nodelay;
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

**Run inside the LXC as root after saving the Nginx configuration:**

```bash
set -euo pipefail
ln -s /etc/nginx/sites-available/solar-dashboard /etc/nginx/sites-enabled/solar-dashboard
nginx -t
systemctl enable --now nginx
```

Prove authentication and TLS from a trusted client. The first check must print `401`, the second
must succeed with HTTP 200, and the third must validate the certificate without `--insecure`.

**Run on a trusted LAN/VPN client; replace the DNS name and username:**

```bash
set -euo pipefail
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  https://DASHBOARD_DNS_NAME/)" = 401
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --user DASHBOARD_USERNAME https://DASHBOARD_DNS_NAME/)" = 200
curl --fail --user DASHBOARD_USERNAME https://DASHBOARD_DNS_NAME/api/status
```

Open `https://DASHBOARD_DNS_NAME/?property=home-01` on a phone. Confirm the chart, property selector,
recommendation, process status, heartbeat freshness, last confirmed delivery and fallback counts.

## 12. Live acceptance checklist

Record evidence without secrets, addresses, coordinates, account identifiers, payloads or serials.

- [ ] LXC is unprivileged; only explicitly required features are enabled.
- [ ] System clock, DNS and certificate validation work.
- [ ] Egress is limited to the selected regional/provider, Influx, syslog, DNS and NTP endpoints.
- [ ] Three distinct buckets and five distinct least-privilege tokens exist.
- [ ] Configuration and all five environment files pass ownership/read-isolation checks.
- [ ] Each scope passes `validate`; the limitation of that command is understood.
- [ ] Telemetry one-shot authenticates to Sigenergy and a plausible confirmed Influx record appears.
- [ ] Tariff one-shot returns only valid intervals of at most two hours and confirmed records appear.
- [ ] A due forecast creates one complete snapshot and a recommendation; reconciliation later
  creates the daily actual-versus-forecast factor.
- [ ] Cleanly stopping/starting each service in turn leaves the other four running.
- [ ] During the authorized SIGKILL test, the selected service automatically returns active, its
  `NRestarts` counter increases, and every peer remains active.
- [ ] During an authorized, one-selected-property Influx outage exercise, a failed direct write
  creates exactly one private SQLite pending record; after recovery it drains and the distinct
  confirmed Influx timestamp count increases by exactly one.
- [ ] Outbox state, WAL/SHM permissions, disk reserve and alerts have been checked; permission
  checks are silent and the expected sanitized status JSON was reviewed separately.
- [ ] Dashboard direct loopback access is not exposed; HTTPS proxy returns unauthenticated 401,
  authenticated 200, and a valid trusted certificate.
- [ ] Dashboard reports all five heartbeats and confirmed-delivery freshness.
- [ ] If enabled, syslog receives fixed events and collection continues when syslog is unavailable.
- [ ] At least several complete days have been compared with the inverter before recommendations
  influence an overnight charging decision.

## Backup, upgrade and rollback

Back up these items through an encrypted, access-controlled system:

- `/etc/solar-battery-forecaster/config.yaml` because it contains private property coordinates.
- The five environment files because they contain service tokens/provider credentials.
- Each writer's entire `/var/lib/solar-battery-*` directory.
- InfluxDB buckets using the existing database operator's tested backup/restore process.
- Nginx authentication and TLS material under the organization's secret/key policy.

For a consistent writer backup or upgrade, stop only that writer, start the matching
`solar-battery-outbox-verify@SCOPE.service` unit, and preserve `outbox.sqlite3` together with any
`outbox.sqlite3-wal` and
`outbox.sqlite3-shm` files. Restart it promptly. Never copy only the main SQLite file while the
writer is active. Follow [`deployment/LXC.md`](../deployment/LXC.md#upgrade-and-rollback) for
verified artifacts, schema compatibility, rollback and uninstall retention.

Rotate an Influx token one service at a time: create a new custom token with the same exact grants,
stop that service, update only its mode-0640 environment file, run scoped validation plus its
relevant read/write acceptance, restart it, and only then revoke the old token. Rotation of the
Sigenergy secret follows the same staged approach if the portal permits overlapping credentials;
otherwise plan the brief telemetry interruption. Octopus and Open-Meteo credentials are not loaded
by the current adapters.

Test restoration on a separate unprivileged LXC. A backup that has not passed a restore exercise is
not production evidence.

## Operations and troubleshooting

Use the mobile status page first, then local journald. Remote syslog intentionally does not contain
arbitrary log text.

**Run inside the LXC as an authorized operator:**

```bash
set -euo pipefail
systemctl --no-pager --full status solar-battery-telemetry
journalctl -u solar-battery-telemetry --since today --no-pager
```

Do not paste journal output into a public issue without reviewing it for private data. The CLI
deliberately returns generic provider failures; correlate timestamp and fixed status codes, then
check provider/application authorization and destination reachability without printing secrets.

Common checks:

| Symptom | Checks and safe response |
|---|---|
| `validate` reports Influx health failure | DNS, route, HTTPS certificate trust, Influx health and the service's token file; do not disable TLS verification |
| Telemetry authentication fails | Correct region, approved Monitoring app, current AppKey/AppSecret, authorized system ID, portal call limits and clock |
| Tariff interval is invalid | Confirm exact product/tariff codes; the current adapter cannot accept intervals longer than two hours |
| No forecast/reconciliation record | Confirm local time zone and due schedule, complete prerequisite data, Influx read grants and status heartbeat |
| Pending outbox grows | Restore Influx safely, inspect `outbox status`/`verify` as that writer, check disk reserve, then use `drain`; never delete the database |
| Dashboard is stale | Check all five heartbeats, dashboard read token grants and confirmed Influx delivery; SQLite pending data is intentionally absent |
| Nginx returns 502 | Confirm dashboard service is running and listening on `127.0.0.1:8088` in the same LXC |
| Syslog reports failures | Check exact destination, firewall and CA trust; collection should remain healthy while syslog is repaired |

The authoritative outbox actions (`status`, `verify`, `drain`, `retry`, and quarantine export) and
their data-handling warnings are documented in
[`deployment/LXC.md`](../deployment/LXC.md#influxdb-buckets-and-token-permissions).

## Official provider references

- [Sigenergy developer portal](https://developer.sigencloud.com/)
- [Sigenergy portal overview and account access](https://developer.sigencloud.com/user/user/manual/68)
- [Sigenergy application creation and Monitoring purpose](https://developer.sigencloud.com/user/user/manual/69)
- [Sigenergy application dashboard and one-time AppSecret](https://developer.sigencloud.com/user/user/manual/70)
- [Sigenergy northbound connection process](https://developer.sigencloud.com/user/user/manual/77)
- [Octopus REST API basics](https://docs.octopus.energy/rest/guides/api-basics/)
- [Octopus GraphQL authentication](https://developer.octopus.energy/graphql/guides/basics)
- [Octopus account API key guidance](https://octopus.energy/help-and-faqs/articles/how-do-i-access-the-octopus-api/)
- [Open-Meteo forecast API](https://open-meteo.com/en/docs)
- [InfluxDB OSS 2.x bucket creation](https://docs.influxdata.com/influxdb/v2/admin/buckets/create-bucket/)
- [InfluxDB OSS 2.x token creation](https://docs.influxdata.com/influxdb/v2/admin/tokens/create-token/)
- [Influx CLI `auth create`](https://docs.influxdata.com/influxdb/v2/reference/cli/influx/auth/create/)
- [Proxmox `pct` reference](https://pve.proxmox.com/pve-docs/pct.1.html)
- [Nginx HTTP basic authentication](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)
- [Nginx HTTP proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)

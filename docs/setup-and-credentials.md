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

| Input | Authoritative source | Secret? | Network destination | Minimum privilege | How to validate | Rotation |
|---|---|---:|---|---|---|---|
| Sigenergy AppKey | Sigenergy developer portal | Yes | Selected regional `openapi-*.sigencloud.com` host | Approved **Monitoring** application only; no Control or VPP dispatch | Run telemetry once and confirm a new `energy_telemetry` point | Create/rotate in the portal, update only `telemetry.env`, test, then retire the old credential if the portal permits overlap |
| Sigenergy AppSecret | Shown once when generated in the developer portal | Yes | Same as AppKey | Same Monitoring application | Same telemetry test | Store immediately; schedule a maintenance window if regeneration invalidates the old secret immediately |
| Sigenergy system ID | Authorized system/site shown by the portal or returned by its documented system API | Treat as sensitive | Same as AppKey | Only the authorized property | Same telemetry test | Replace if the installation/system identity changes |
| Octopus product and tariff codes | Property's Octopus account/contract and public product API | No, but account context is private | `api.octopus.energy:443` | Public standard-unit-rate endpoint | Run tariff once and confirm `electricity_tariff` points with expected dates/prices | Recheck whenever the tariff changes |
| Octopus account API key | Octopus account Developer settings | Yes | Not used by version 0.1 | Do not install it for the current REST adapter | Not applicable | Manage in the Octopus account when a future authenticated adapter explicitly requires it |
| Open-Meteo forecast | Public non-commercial API | No key in version 0.1 | `api.open-meteo.com:443` | Public forecast endpoint | Run forecast-plan when due and confirm a complete `pv_forecast` snapshot | Not applicable |
| Five InfluxDB custom tokens | Existing InfluxDB OSS 2.x administrator | Yes | Your InfluxDB HTTPS endpoint | Exact bucket grants listed below | Validate each scope, then prove read/write grants with its one-shot service test | Replace one service token at a time, verify, then revoke the old token |
| Syslog destination | Your log-platform administrator | Hostname is not a secret; trust material may be sensitive | Configured UDP/TCP/TLS host and port | Receive only fixed operational event codes | Induce a non-sensitive test event and confirm receipt | Follow the log platform's certificate/trust rotation process |

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

This application targets InfluxDB OSS 2.x. Confirm the existing server version and health before
creating resources.

**Run on the InfluxDB host or an administrator workstation with the Influx CLI configured:**

```bash
influx version
influx ping --host https://influxdb.example.invalid:8086
```

Create three distinct buckets. Select retention periods that match your privacy and forecasting
needs; reconciliation needs enough telemetry and planning history to learn useful factors. The
official references are [Create a bucket](https://docs.influxdata.com/influxdb/v2/admin/buckets/create-bucket/)
and [Create a custom API token](https://docs.influxdata.com/influxdb/v2/admin/tokens/create-token/).

**Run on the InfluxDB administrator workstation; replace every uppercase placeholder first:**

```bash
influx bucket create --org-id ORG_ID --name solar_telemetry --retention RETENTION_DURATION
influx bucket create --org-id ORG_ID --name solar_tariff --retention RETENTION_DURATION
influx bucket create --org-id ORG_ID --name solar_planning --retention RETENTION_DURATION
influx bucket list --org-id ORG_ID
```

Record the three bucket IDs from the list. Create five different custom tokens with the exact
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

The minimum tested sizing is one vCPU, 512 MB RAM, and 4 GB disk. Use one vCPU, 1 GB RAM, and 8 GB
disk for operational headroom and for the co-located Nginx proxy described below. Increase disk
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
apt-get update
apt-get upgrade
apt-get install ca-certificates curl python3 python3-venv
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
issuing CA in the LXC trust store. Permit only that destination through the firewall. Collection
continues if syslog is slow or unavailable; queue loss/failure appears in the status view.

## 10. Validate and start all five services

First run scoped configuration/Influx health validation under each service identity. This proves
that the relevant environment can load and the Influx endpoint responds, but not provider access
or exact bucket grants.

**Run inside the LXC as root; repeat with each matching identity, file and scope:**

```bash
sudo -u solar-telemetry sh -c '
  set -a
  . /etc/solar-battery-forecaster/telemetry.env
  set +a
  exec /opt/solar-battery-forecaster/.venv/bin/solar-battery-forecaster \
    validate --scope telemetry --config /etc/solar-battery-forecaster/config.yaml
'
```

Run live one-shot telemetry and tariff collection. These calls contact the real providers and can
write to InfluxDB or, on write failure, to the fallback outbox.

**Run inside the LXC as root during the acceptance window:**

```bash
sudo -u solar-telemetry sh -c '
  set -a; . /etc/solar-battery-forecaster/telemetry.env; set +a
  exec /opt/solar-battery-forecaster/.venv/bin/solar-battery-forecaster \
    telemetry --config /etc/solar-battery-forecaster/config.yaml --once
'
sudo -u solar-tariff sh -c '
  set -a; . /etc/solar-battery-forecaster/tariff.env; set +a
  exec /opt/solar-battery-forecaster/.venv/bin/solar-battery-forecaster \
    tariff --config /etc/solar-battery-forecaster/config.yaml --once
'
```

Confirm new, plausible, correctly timestamped `energy_telemetry` and `electricity_tariff` records
in InfluxDB. A zero exit alone is insufficient. Forecast planning is schedule-aware and normally
targets tomorrow only after the configured local forecast time; reconciliation is also due-time
aware. Run/observe those processes at their configured due times, then confirm a complete
`pv_forecast`, a `battery_decision`, and later a `pv_daily` record. Review the displayed values
against the physical system and provider portals.

Enable all five independent services only after the one-shot checks succeed.

**Run inside the LXC as root:**

```bash
systemctl enable --now solar-battery-telemetry
systemctl enable --now solar-battery-tariff
systemctl enable --now solar-battery-forecast-plan
systemctl enable --now solar-battery-reconciliation
systemctl enable --now solar-battery-dashboard
systemctl --no-pager --full status \
  solar-battery-telemetry solar-battery-tariff solar-battery-forecast-plan \
  solar-battery-reconciliation solar-battery-dashboard
```

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
ln -s /etc/nginx/sites-available/solar-dashboard /etc/nginx/sites-enabled/solar-dashboard
nginx -t
systemctl enable --now nginx
```

Prove authentication and TLS from a trusted client. The first check must print `401`, the second
must succeed with HTTP 200, and the third must validate the certificate without `--insecure`.

**Run on a trusted LAN/VPN client; replace the DNS name and username:**

```bash
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
- [ ] Stopping/killing each service in turn leaves the other four running.
- [ ] During an authorized Influx outage, a failed direct write creates a private SQLite pending
  record; after recovery it drains and the confirmed Influx point appears once.
- [ ] Outbox state, WAL/SHM permissions, disk reserve and alerts have been checked.
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

For a consistent writer backup or upgrade, stop only that writer, run `outbox verify` as its service
identity, and preserve `outbox.sqlite3` together with any `outbox.sqlite3-wal` and
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
- [Octopus REST API basics](https://docs.octopus.energy/rest/guides/api-basics/)
- [Octopus GraphQL authentication](https://developer.octopus.energy/graphql/guides/basics)
- [Octopus account API key guidance](https://octopus.energy/help-and-faqs/articles/how-do-i-access-the-octopus-api/)
- [Open-Meteo forecast API](https://open-meteo.com/en/docs)
- [InfluxDB OSS 2.x bucket creation](https://docs.influxdata.com/influxdb/v2/admin/buckets/create-bucket/)
- [InfluxDB OSS 2.x token creation](https://docs.influxdata.com/influxdb/v2/admin/tokens/create-token/)
- [Influx CLI `auth create`](https://docs.influxdata.com/influxdb/v2/reference/cli/influx/auth/create/)
- [Proxmox `pct` reference](https://pve.proxmox.com/pve-docs/pct.1.html)

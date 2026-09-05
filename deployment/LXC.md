# Debian LXC deployment

Use an unprivileged Debian 12 LXC with one vCPU, 512 MB RAM, 4 GB disk, accurate time, outbound
HTTPS, and access only to the dedicated InfluxDB endpoint. Do not expose the collector publicly.

## Release inputs

Deploy a reviewed release, never a moving branch. Download the release wheel, source archive, and
publisher-generated `SHA256SUMS` from the same GitHub release. Verify both artifacts before any
installation. Replace `VERSION` below with an exact signed release tag.

```bash
mkdir /tmp/solar-release
cd /tmp/solar-release
curl --fail --location --remote-name \
  https://github.com/soothill/solar-battery-forecaster/releases/download/VERSION/solar_battery_forecaster-0.1.0-py3-none-any.whl
curl --fail --location --remote-name \
  https://github.com/soothill/solar-battery-forecaster/releases/download/VERSION/solar_battery_forecaster-0.1.0.tar.gz
curl --fail --location --remote-name \
  https://github.com/soothill/solar-battery-forecaster/releases/download/VERSION/SHA256SUMS
sha256sum --check SHA256SUMS
```

The protected release workflow must publish these checksums from the reviewed commit. Signature
verification and required GitHub checks are repository controls; a text file claiming approval is
not a substitute.

## Install without a privileged build

Extract the verified source archive only for locked dependencies, example configuration, and
service units. The application itself is installed from the verified wheel, so root never invokes
a build backend or resolves `build-system.requires`.

```bash
apt-get update
apt-get install -y ca-certificates python3 python3-venv
useradd --system --home /opt/solar-battery-forecaster --shell /usr/sbin/nologin solarplanner
mkdir -p /opt/solar-battery-forecaster
tar -xzf /tmp/solar-release/solar_battery_forecaster-0.1.0.tar.gz \
  --strip-components=1 -C /opt/solar-battery-forecaster
python3 -m venv /opt/solar-battery-forecaster/.venv
/opt/solar-battery-forecaster/.venv/bin/pip install --require-hashes \
  -r /opt/solar-battery-forecaster/requirements.lock
/opt/solar-battery-forecaster/.venv/bin/pip install --no-deps --no-build-isolation \
  /tmp/solar-release/solar_battery_forecaster-0.1.0-py3-none-any.whl
install -d -o root -g solarplanner -m 0750 /etc/solar-battery-forecaster
cp /opt/solar-battery-forecaster/config.example.yaml /etc/solar-battery-forecaster/config.yaml
for scope in telemetry tariff forecast-plan reconciliation dashboard; do
  install -o root -g solarplanner -m 0640 \
    "/opt/solar-battery-forecaster/deployment/environment/${scope}.env.example" \
    "/etc/solar-battery-forecaster/${scope}.env"
done
chown root:solarplanner /etc/solar-battery-forecaster/config.yaml
chmod 0640 /etc/solar-battery-forecaster/config.yaml
cp /opt/solar-battery-forecaster/deployment/*.service /etc/systemd/system/
systemctl daemon-reload
```

Edit the configuration and five environment files. Each process resolves only its own token and
provider secrets. Validate one scope without loading any other scope's environment:

```bash
sudo -u solarplanner sh -c '
  set -a
  . "/etc/solar-battery-forecaster/$1.env"
  set +a
  exec /opt/solar-battery-forecaster/.venv/bin/solar-battery-forecaster \
    validate --scope "$1" --config /etc/solar-battery-forecaster/config.yaml
' validate telemetry
```

Repeat the final argument for `tariff`, `forecast-plan`, `reconciliation`, and `dashboard`.

## InfluxDB buckets and token permissions

InfluxDB OSS 2.x permissions are bucket-level, so use the three configured buckets rather than
claiming measurement-level isolation inside one bucket:

| Process | Read | Write | Provider secret |
|---|---|---|---|
| telemetry | none | `solar_telemetry` | read-only Sigenergy AppKey, secret, system ID |
| tariff | none | `solar_tariff` | none for public REST; future tariff credential only here |
| forecast-plan | all three buckets | `solar_planning` | forecast provider credential only if required |
| reconciliation | `solar_telemetry`, `solar_planning` | `solar_planning` | none |
| dashboard | all three buckets | none | none |

Create five distinct Influx authorizations with exactly those bucket grants. The forecast-plan and
reconciliation tokens need read and write on `solar_planning`; they do not need provider secrets.

Start each failure-isolated process independently:

```bash
systemctl enable --now solar-battery-telemetry
systemctl enable --now solar-battery-tariff
systemctl enable --now solar-battery-forecast-plan
systemctl enable --now solar-battery-reconciliation
systemctl enable --now solar-battery-dashboard
```

The units do not require or restart one another. The dashboard listens on `127.0.0.1:8088`; use an
authenticated HTTPS reverse proxy for phone access. Direct `0.0.0.0` binding is suitable only on a
trusted private LAN with firewall restrictions because version 0.1 has no authentication.

## Upgrade and rollback

Stop only the service being upgraded, verify the new release artifacts, install the new wheel with
the same `--no-deps --no-build-isolation` command, and restart it. Roll back by reinstalling the
previous verified wheel. Schema additions are append-only in version 0.1; old measurements remain
readable.

Repository tests verify the five unit-file contracts and absence of `Requires`/`PartOf` coupling.
Deployment acceptance must additionally stop and kill each live service in turn and confirm that
the other four remain running; static repository tests cannot prove the host service manager's
runtime behaviour.

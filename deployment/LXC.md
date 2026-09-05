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
not a substitute. CI installs the frozen development lock, including the exact build backend
version declared in `pyproject.toml`, and runs the build with both `--frozen` and `--no-isolation`
so it cannot resolve an unreviewed backend at build time.

## Install without a privileged build

Extract the verified source archive only for locked dependencies, example configuration, and
service units. The application itself is installed from the verified wheel, so root never invokes
a build backend or resolves `build-system.requires`.

```bash
apt-get update
apt-get install -y ca-certificates python3 python3-venv
groupadd --system solar-config
for identity in solar-telemetry solar-tariff solar-forecast-plan solar-reconciliation solar-dashboard; do
  groupadd --system "$identity"
  useradd --system --home /nonexistent --shell /usr/sbin/nologin \
    --gid "$identity" --groups solar-config "$identity"
done
mkdir -p /opt/solar-battery-forecaster
tar -xzf /tmp/solar-release/solar_battery_forecaster-0.1.0.tar.gz \
  --strip-components=1 -C /opt/solar-battery-forecaster
python3 -m venv /opt/solar-battery-forecaster/.venv
/opt/solar-battery-forecaster/.venv/bin/pip install --require-hashes \
  -r /opt/solar-battery-forecaster/requirements.lock
/opt/solar-battery-forecaster/.venv/bin/pip install --no-deps --no-build-isolation \
  /tmp/solar-release/solar_battery_forecaster-0.1.0-py3-none-any.whl
chown -R root:solar-config /opt/solar-battery-forecaster
chmod -R g+rX,o-rwx /opt/solar-battery-forecaster
install -d -o root -g solar-config -m 0750 /etc/solar-battery-forecaster
cp /opt/solar-battery-forecaster/config.example.yaml /etc/solar-battery-forecaster/config.yaml
install -o root -g solar-telemetry -m 0640 \
  /opt/solar-battery-forecaster/deployment/environment/telemetry.env.example \
  /etc/solar-battery-forecaster/telemetry.env
install -o root -g solar-tariff -m 0640 \
  /opt/solar-battery-forecaster/deployment/environment/tariff.env.example \
  /etc/solar-battery-forecaster/tariff.env
install -o root -g solar-forecast-plan -m 0640 \
  /opt/solar-battery-forecaster/deployment/environment/forecast-plan.env.example \
  /etc/solar-battery-forecaster/forecast-plan.env
install -o root -g solar-reconciliation -m 0640 \
  /opt/solar-battery-forecaster/deployment/environment/reconciliation.env.example \
  /etc/solar-battery-forecaster/reconciliation.env
install -o root -g solar-dashboard -m 0640 \
  /opt/solar-battery-forecaster/deployment/environment/dashboard.env.example \
  /etc/solar-battery-forecaster/dashboard.env
chown root:solar-config /etc/solar-battery-forecaster/config.yaml
chmod 0640 /etc/solar-battery-forecaster/config.yaml
cp /opt/solar-battery-forecaster/deployment/*.service /etc/systemd/system/
systemctl daemon-reload
```

Edit the shared non-secret configuration and five service-private environment files. Each process
resolves only its own token and provider secrets. Validate one scope as that service identity,
without loading any other scope's environment:

```bash
sudo -u solar-telemetry sh -c '
  set -a
  . /etc/solar-battery-forecaster/telemetry.env
  set +a
  exec /opt/solar-battery-forecaster/.venv/bin/solar-battery-forecaster \
    validate --scope telemetry --config /etc/solar-battery-forecaster/config.yaml
'
```

Repeat using the matching identity, environment file, and scope for `tariff`, `forecast-plan`,
`reconciliation`, and `dashboard`.

Before starting services, verify Unix read isolation without printing any secret values:

```bash
check_isolation() {
  identity="$1"
  own="$2"
  sudo -u "$identity" test -r /etc/solar-battery-forecaster/config.yaml
  sudo -u "$identity" test -r "/etc/solar-battery-forecaster/$own.env"
  for peer in telemetry tariff forecast-plan reconciliation dashboard; do
    if [ "$peer" != "$own" ]; then
      sudo -u "$identity" test ! -r "/etc/solar-battery-forecaster/$peer.env"
    fi
  done
}
check_isolation solar-telemetry telemetry
check_isolation solar-tariff tariff
check_isolation solar-forecast-plan forecast-plan
check_isolation solar-reconciliation reconciliation
check_isolation solar-dashboard dashboard
```

All commands must exit zero. Also inspect `namei -l` for the configuration directory and confirm
no service user is a member of another service's private group.

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

Repository tests verify the five unit-file contracts, distinct identities and secret files, and
absence of `Requires`/`PartOf` coupling. Deployment acceptance must additionally run the permission
checks above, stop and kill each live service in turn, and confirm that the other four remain
running; static repository tests cannot prove host identities or service-manager runtime behaviour.

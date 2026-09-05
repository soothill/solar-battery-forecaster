# Debian LXC deployment

Use an unprivileged Debian 12 LXC with one vCPU, 512 MB RAM, 4 GB disk, accurate time, outbound
HTTPS, and access only to the dedicated InfluxDB endpoint. Do not expose the collector publicly.

## Release inputs

Deploy a reviewed release, never a moving branch. Perform download and provenance verification on
a trusted administrator workstation, then transfer only the verified inputs into the LXC. Install
a current [GitHub CLI](https://cli.github.com/) from its official distribution; Debian's base
packages are not assumed to provide a sufficiently recent version. Confirm that
`gh attestation verify --help` lists `--signer-workflow`, `--source-ref`, and `--source-digest`.

Record the exact 40-character reviewed source commit independently of the release download, then
download the wheel, source archive, and publisher-generated `SHA256SUMS`. Replace the two
placeholders below before running these commands on the trusted workstation.

```bash
release_version=v0.1.0
source_commit=REPLACE_WITH_40_CHARACTER_REVIEWED_COMMIT_SHA
repository=soothill/solar-battery-forecaster
gh attestation verify --help >/dev/null
mkdir solar-release
cd solar-release
gh release download "$release_version" --repo "$repository" \
  --pattern 'solar_battery_forecaster-*.whl' \
  --pattern 'solar_battery_forecaster-*.tar.gz' \
  --pattern SHA256SUMS
for artifact in solar_battery_forecaster-*.whl solar_battery_forecaster-*.tar.gz SHA256SUMS; do
  gh attestation verify "$artifact" \
    --repo "$repository" \
    --signer-workflow soothill/solar-battery-forecaster/.github/workflows/release.yml \
    --source-ref "refs/tags/$release_version" \
    --source-digest "$source_commit" \
    --deny-self-hosted-runners
done
sha256sum --check SHA256SUMS
```

Do not install if any attestation fails its repository, exact workflow identity, tag ref, source
commit digest, or hosted-runner check. `SHA256SUMS` is itself attested, but because it travels in
the same release channel as the artifacts it is only a supplemental corruption/completeness check,
not independent proof of authenticity.

After every attestation and checksum succeeds, transfer the three verified files over a trusted
administrative channel into `/tmp/solar-release` inside the LXC. Run `sha256sum --check
SHA256SUMS` again in that directory after transfer and before installation. The GitHub CLI is not
required inside the LXC when verification is completed on the trusted workstation.

The tag-only release workflow accepts only an exact project version whose signed commit is on
`origin/main`. It tests, scans, builds with the frozen development lock and exact build backend,
attests every release input through Sigstore and GitHub, then publishes through a separate
`contents: write` job. Protect version tags and the GitHub `release` environment; signature
verification, required checks, and environment approval are repository controls, and a text file
claiming approval is not a substitute.

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
chmod -R g-w /opt/solar-battery-forecaster
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

The shared application tree is readable and executable by `solar-config` members but explicitly
not group-writable. Only root may replace installed code or the virtual environment.

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

Each provider/worker unit has `MemoryMax=80M` and the dashboard has `MemoryMax=96M`. Their 416 MB
aggregate ceiling leaves 96 MB of a 512 MB LXC for the OS and service manager while bounding a
single malformed response or process fault. InfluxDB is assumed to run outside this LXC. A process
that reaches its cgroup ceiling is killed and then restarted by its own `Restart=on-failure` policy,
without coupling peer services. Verify these limits with `systemctl show -p MemoryMax
solar-battery-telemetry solar-battery-tariff solar-battery-forecast-plan
solar-battery-reconciliation solar-battery-dashboard`; increase the LXC allocation, rather than
silently removing a unit limit, if Linux soak tests at the intended property/array count and
dashboard concurrency show sustained `MemoryCurrent` close to a ceiling.

## Upgrade and rollback

Stop only the service being upgraded, verify the new release artifacts, install the new wheel with
the same `--no-deps --no-build-isolation` command, and restart it. Roll back by reinstalling the
previous verified wheel. Schema additions are append-only in version 0.1; old measurements remain
readable.

Repository tests verify the five unit-file contracts, distinct identities and secret files, and
absence of `Requires`/`PartOf` coupling. Deployment acceptance must additionally run the permission
checks above, stop and kill each live service in turn, and confirm that the other four remain
running; static repository tests cannot prove host identities or service-manager runtime behaviour.

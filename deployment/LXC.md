# Debian LXC deployment

Use an unprivileged Debian 12 LXC with one vCPU, accurate time, controlled outbound HTTPS, and
network access to the dedicated InfluxDB endpoint. The 512 MB RAM/4 GB disk sizing is an unverified
minimum baseline derived from service limits; use the live-tested/recommended 1 GB RAM/8 GB disk
baseline for operational headroom or a co-located authenticated Nginx proxy. Do not expose the
collector publicly. For the complete operator journey and provider credential onboarding, read
[`docs/setup-and-credentials.md`](../docs/setup-and-credentials.md); this file remains the canonical
hardened release and service installation reference.

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
apt-get install -y ca-certificates python3 python3-venv util-linux
groupadd --system solar-config
groupadd --system solar-observe
for identity in solar-telemetry solar-tariff solar-forecast-plan solar-reconciliation; do
  groupadd --system "$identity"
  useradd --system --home /nonexistent --shell /usr/sbin/nologin \
    --gid "$identity" --groups solar-config "$identity"
done
groupadd --system solar-dashboard
useradd --system --home /nonexistent --shell /usr/sbin/nologin \
  --gid solar-dashboard --groups solar-config,solar-observe solar-dashboard
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
cp /opt/solar-battery-forecaster/deployment/maintenance/*.service /etc/systemd/system/
install -o root -g root -m 0644 \
  /opt/solar-battery-forecaster/deployment/solar-battery-status.tmpfiles \
  /etc/tmpfiles.d/solar-battery-status.conf
systemd-tmpfiles --create /etc/tmpfiles.d/solar-battery-status.conf
systemctl daemon-reload
```

The shared application tree is readable and executable by `solar-config` members but explicitly
not group-writable. Only root may replace installed code or the virtual environment.

Edit the shared non-secret configuration and five service-private environment files. Each process
resolves only its own token and provider secrets. The installed validation template uses systemd's
`EnvironmentFile=` parser and direct process execution; it never interprets the secret file as
shell code. Validate every scope as its matching identity:

```bash
for scope in telemetry tariff forecast-plan reconciliation dashboard; do
  systemctl start "solar-battery-validate@$scope.service"
  systemctl show "solar-battery-validate@$scope.service" \
    --property=Result --property=ExecMainStatus --no-pager
done
```

Every instance must report `Result=success` and `ExecMainStatus=0`. This checks configuration and
InfluxDB health, not provider access or exact bucket grants. Maintenance templates are root-owned,
non-enabled, hardened one-shot units; only an administrator starts them. Never source an
`EnvironmentFile` in a shell because provider values are data, not trusted shell syntax.

Before starting services, verify Unix read isolation without printing any secret values:

```bash
check_isolation() {
  identity="$1"
  own="$2"
  runuser -u "$identity" -- test -r /etc/solar-battery-forecaster/config.yaml
  runuser -u "$identity" -- test -r "/etc/solar-battery-forecaster/$own.env"
  for peer in telemetry tariff forecast-plan reconciliation dashboard; do
    if [ "$peer" != "$own" ]; then
      runuser -u "$identity" -- test ! -r "/etc/solar-battery-forecaster/$peer.env"
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

The `solar-observe` group is a one-way read boundary: only `solar-dashboard` is a member. Each
worker owns its setgid mode-2750 runtime directory with that group, writes one atomic mode-0640 sanitized
status projection, and cannot read a peer directory. Verify this ownership and membership before
starting the services.

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

The four writer units create separate mode-0700 state directories under `/var/lib` and run with
`UMask=0077`; the dashboard creates no state directory. Confirm the database and any `-wal`/`-shm`
sidecars are owned by the matching service identity and have no group/other permission:

```bash
for worker in telemetry tariff forecast-plan reconciliation; do
  state="/var/lib/solar-battery-$worker"
  namei -l "$state/outbox.sqlite3"
  find "$state" -maxdepth 1 -type f -perm /077 -print -quit | grep -q . && exit 1 || true
done
```

Each provider collection is admitted only while the configured record, byte, journal-headroom,
collection-reserve, and filesystem-free-space limits can be maintained. There is no automatic age
expiry or eviction. Alert on a nonzero pending count, any quarantine/blocked stream, repeated
delivery failures, or declining free space. An empty SQLite schema/control database can exist while
the worker is healthy, but confirmed direct writes create no payload rows. The dashboard deliberately
cannot read peer-private state and displays only confirmed InfluxDB freshness. Stop the matching
writer before a maintenance action that can modify its database. Read scoped status through the
non-enabled one-shot unit without exposing a peer token:

```bash
systemctl start solar-battery-outbox-status@telemetry.service
journalctl -u solar-battery-outbox-status@telemetry.service -o cat --no-pager \
  | grep '^{' | tail -n 1
```

Installed templates also provide `solar-battery-outbox-verify@SCOPE.service` and
`solar-battery-outbox-drain@SCOPE.service`. The application additionally supports `retry` and
`export-quarantine --output NEW_PATH`; those exceptional actions require a separately reviewed,
direct-execution maintenance unit rather than shell-loading the secret file.
`drain` deliberately bypasses the current retry timer but keeps record/byte limits. `retry` resets
network-delivery attempts; it does not release checksum quarantine. Exports contain exact line
protocol and property identifiers, are created mode 0600, and must be encrypted and access-limited
as customer operational data. Never paste their contents into logs or issues.

The units do not require or restart one another. The dashboard listens on `127.0.0.1:8088`; use an
authenticated HTTPS reverse proxy for phone access. Direct `0.0.0.0` binding is suitable only on a
trusted private LAN with firewall restrictions because version 0.1 has no authentication.

The dashboard's **System status** section and `GET /api/status` show all five process heartbeats,
last cycle result, last locally accepted item, last confirmed InfluxDB delivery, fallback totals,
and up to 50 fixed-code operational events per process. Snapshots refresh every 30 seconds and become
stale after 90 seconds. The dashboard reads only the five fixed status files; it cannot read SQLite
fallback databases or the system journal. These snapshots are operational hints, not an audit log,
and disappear when `/run` is recreated.

Remote syslog is disabled by default. To enable it, set `observability.syslog.enabled: true` and
configure the host, port, and `udp`, `tcp`, or `tls` transport. TLS uses the system CA store and
verifies its chain and hostname against the configured `host`. Version 0.1 does not present a client
certificate and therefore does not support mTLS. Permit only the selected destination in the LXC firewall.
Forwarding has a bounded queue and retry backoff: loss, delay, or overflow is reported in status but
never changes collection success. Only fixed, allowlisted structured events are sent; arbitrary
application log messages remain in journald. Status and syslog events never contain property IDs,
exception messages, tokens, coordinates, account identifiers, raw provider payloads, or fallback
contents.

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

Stop only the service being upgraded, start its
`solar-battery-outbox-verify@SCOPE.service` unit, and back up its database plus WAL/SHM sidecars
while stopped. Verify the new release artifacts, install the new wheel with the
same `--no-deps --no-build-isolation` command, and restart it. Roll back only to a verified wheel
whose documented outbox schema supports the on-disk `PRAGMA user_version`; preserve the complete
state directory throughout. Version 0.1 uses schema 1 and fails closed on unknown versions rather
than rewriting them. Old Influx measurements remain readable.

Package uninstall must stop and disable the four writers before removing code or units. Preserve
their `/var/lib/solar-battery-*` directories by default so pending and quarantined customer data is
recoverable. Delete those directories only under an explicit retention decision after verified
drain/export and backup; ordinary rollback or uninstall never removes them.

Repository tests verify the five unit-file contracts, distinct identities and secret files, and
absence of `Requires`/`PartOf` coupling. Deployment acceptance must additionally run the permission
checks above, stop and kill each live service in turn, and confirm that the other four remain
running; static repository tests cannot prove host identities or service-manager runtime behaviour.

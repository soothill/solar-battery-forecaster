# Debian LXC deployment

## Suggested container

- Unprivileged Debian 12 LXC
- 1 vCPU, 512 MB RAM, 4 GB disk
- A fixed DHCP lease or static address
- Outbound HTTPS, access to InfluxDB, and accurate host-provided time

On the Proxmox host, create the container using the normal UI or your standard template.
Do not expose the collector directly to the internet.

## Install the application

Run as root inside the new container:

```bash
apt-get update
apt-get install -y ca-certificates git python3 python3-venv
useradd --system --home /opt/solar-battery-forecaster --shell /usr/sbin/nologin solarplanner
git clone --branch v0.1.0 https://github.com/soothill/solar-battery-forecaster.git /opt/solar-battery-forecaster
python3 -m venv /opt/solar-battery-forecaster/.venv
/opt/solar-battery-forecaster/.venv/bin/pip install --require-hashes \
  -r /opt/solar-battery-forecaster/requirements.lock
/opt/solar-battery-forecaster/.venv/bin/pip install --no-deps /opt/solar-battery-forecaster
install -d -o root -g solarplanner -m 0750 /etc/solar-battery-forecaster
cp /opt/solar-battery-forecaster/config.example.yaml /etc/solar-battery-forecaster/config.yaml
cp /opt/solar-battery-forecaster/.env.example /etc/solar-battery-forecaster/environment
chown root:solarplanner /etc/solar-battery-forecaster/config.yaml \
  /etc/solar-battery-forecaster/environment
chmod 0640 /etc/solar-battery-forecaster/config.yaml
chmod 0640 /etc/solar-battery-forecaster/environment
cp /opt/solar-battery-forecaster/deployment/solar-battery-forecaster.service /etc/systemd/system/
cp /opt/solar-battery-forecaster/deployment/solar-battery-dashboard.service /etc/systemd/system/
systemctl daemon-reload
```

Edit the two files in `/etc/solar-battery-forecaster`, validate, and start:

```bash
set -a
. /etc/solar-battery-forecaster/environment
set +a
sudo -u solarplanner /opt/solar-battery-forecaster/.venv/bin/solar-battery-forecaster \
  validate --config /etc/solar-battery-forecaster/config.yaml
systemctl enable --now solar-battery-forecaster
systemctl enable --now solar-battery-dashboard
journalctl -u solar-battery-forecaster -f
```

The dashboard listens only on `127.0.0.1:8088`. Put it behind your existing authenticated
HTTPS reverse proxy for phone access. If you intentionally expose it directly on a trusted
private LAN, change the dashboard unit to `--host 0.0.0.0`; do not expose that listener to
the public internet because version 0.1 does not include user authentication.

## Upgrade

Upgrade only to a reviewed release tag. Replace `vNEXT` with that exact tag:

```bash
systemctl stop solar-battery-forecaster
git -C /opt/solar-battery-forecaster fetch --tags
git -C /opt/solar-battery-forecaster checkout --detach vNEXT
/opt/solar-battery-forecaster/.venv/bin/pip install --require-hashes \
  -r /opt/solar-battery-forecaster/requirements.lock
/opt/solar-battery-forecaster/.venv/bin/pip install --no-deps --upgrade \
  /opt/solar-battery-forecaster
systemctl start solar-battery-forecaster
```

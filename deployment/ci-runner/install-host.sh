#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install-host.sh must run as root" >&2
  exit 1
fi
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
for command in docker gh jq openssl iptables apparmor_parser systemd-creds sha256sum; do
  command -v "$command" >/dev/null
done

install -d -o root -g root -m 0750 /usr/local/libexec/solar-ci-runner
for file in accept-host.sh acquire-and-run.sh check-acceptance.sh \
  mint-github-app-token.sh proxy-down.sh proxy-up.sh run-once.sh \
  validate-host.sh validate-runner.sh verify-fork-policy.sh; do
  install -o root -g root -m 0750 "$script_dir/$file" \
    "/usr/local/libexec/solar-ci-runner/$file"
done
install -d -o root -g root -m 0750 /etc/solar-ci-runner
install -d -o root -g root -m 0700 /etc/credstore.encrypted
if [ ! -e /etc/solar-ci-runner/images.env ]; then
  install -o root -g root -m 0600 "$script_dir/images.env.example" \
    /etc/solar-ci-runner/images.env
fi
if [ ! -e /etc/solar-ci-runner/runner.env ]; then
  install -o root -g root -m 0600 "$script_dir/runner.env.example" \
    /etc/solar-ci-runner/runner.env
fi
install -o root -g root -m 0644 "$script_dir/solar-ci-runner.apparmor" \
  /etc/apparmor.d/solar-ci-runner
install -o root -g root -m 0644 "$script_dir/solar-ci-proxy.apparmor" \
  /etc/apparmor.d/solar-ci-proxy
apparmor_parser -r /etc/apparmor.d/solar-ci-runner
apparmor_parser -r /etc/apparmor.d/solar-ci-proxy
for unit in solar-ci-policy-check.service solar-ci-proxy.service \
  solar-ci-runner.service solar-ci-runner.timer; do
  install -o root -g root -m 0644 "$script_dir/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
rm -f /var/lib/solar-ci-runner/acceptance.ok
echo "files installed but inactive; configure credentials, then run accept-host.sh"

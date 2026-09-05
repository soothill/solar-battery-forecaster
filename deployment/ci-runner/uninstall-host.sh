#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "uninstall-host.sh must run as root" >&2
  exit 1
fi
systemctl disable --now solar-ci-runner.timer 2>/dev/null || true
systemctl stop solar-ci-runner.service solar-ci-proxy.service 2>/dev/null || true
systemctl stop solar-ci-policy-check.service 2>/dev/null || true
/usr/local/libexec/solar-ci-runner/proxy-down.sh 2>/dev/null || true
apparmor_parser -R /etc/apparmor.d/solar-ci-runner 2>/dev/null || true
apparmor_parser -R /etc/apparmor.d/solar-ci-proxy 2>/dev/null || true
rm -f /etc/systemd/system/solar-ci-runner.timer
rm -f /etc/systemd/system/solar-ci-runner.service
rm -f /etc/systemd/system/solar-ci-proxy.service
rm -f /etc/systemd/system/solar-ci-policy-check.service
rm -f /etc/apparmor.d/solar-ci-runner /etc/apparmor.d/solar-ci-proxy
for file in accept-host.sh acquire-and-run.sh check-acceptance.sh \
  mint-github-app-token.sh proxy-down.sh proxy-up.sh run-once.sh \
  validate-host.sh validate-runner.sh verify-fork-policy.sh; do
  rm -f "/usr/local/libexec/solar-ci-runner/$file"
done
rmdir /usr/local/libexec/solar-ci-runner 2>/dev/null || true
rm -f /var/lib/solar-ci-runner/acceptance.ok
rmdir /var/lib/solar-ci-runner 2>/dev/null || true
systemctl daemon-reload
echo "services removed; preserved /etc/solar-ci-runner and encrypted credentials for recovery"

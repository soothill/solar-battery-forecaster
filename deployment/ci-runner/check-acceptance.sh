#!/bin/sh
set -eu

: "${ACCEPTANCE_MARKER:=/var/lib/solar-ci-runner/acceptance.ok}"

fingerprint="$({
  sha256sum \
    /etc/solar-ci-runner/images.env \
    /etc/solar-ci-runner/runner.env \
    /etc/apparmor.d/solar-ci-runner \
    /etc/apparmor.d/solar-ci-proxy \
    /etc/systemd/system/solar-ci-runner.service \
    /etc/systemd/system/solar-ci-runner.timer \
    /etc/systemd/system/solar-ci-proxy.service \
    /etc/systemd/system/solar-ci-policy-check.service \
    /usr/local/libexec/solar-ci-runner/accept-host.sh \
    /usr/local/libexec/solar-ci-runner/acquire-and-run.sh \
    /usr/local/libexec/solar-ci-runner/check-acceptance.sh \
    /usr/local/libexec/solar-ci-runner/mint-github-app-token.sh \
    /usr/local/libexec/solar-ci-runner/proxy-down.sh \
    /usr/local/libexec/solar-ci-runner/proxy-up.sh \
    /usr/local/libexec/solar-ci-runner/run-once.sh \
    /usr/local/libexec/solar-ci-runner/validate-host.sh \
    /usr/local/libexec/solar-ci-runner/validate-runner.sh \
    /usr/local/libexec/solar-ci-runner/verify-fork-policy.sh
} | sha256sum | cut -d ' ' -f 1)"

if [ "${1:-}" = --print ]; then
  printf '%s\n' "$fingerprint"
  exit 0
fi

test -r "$ACCEPTANCE_MARKER"
expected="$(sed -n 's/^fingerprint=\([0-9a-f]\{64\}\)$/\1/p' "$ACCEPTANCE_MARKER")"
test -n "$expected"
test "$fingerprint" = "$expected"

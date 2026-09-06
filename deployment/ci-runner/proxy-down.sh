#!/bin/sh
set -eu

: "${PROXY_NAME:=solar-ci-proxy}"
: "${RUNNER_NETWORK:=solar-ci-isolated}"
: "${EGRESS_NETWORK:=solar-ci-egress}"

docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
egress_subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "$EGRESS_NETWORK" 2>/dev/null || true)"
runner_subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "$RUNNER_NETWORK" 2>/dev/null || true)"
if [ -n "$egress_subnet" ]; then
  iptables -D INPUT -s "$egress_subnet" -j REJECT 2>/dev/null || true
fi
if [ -n "$runner_subnet" ]; then
  iptables -D INPUT -s "$runner_subnet" -j REJECT 2>/dev/null || true
fi
iptables -D DOCKER-USER -j SOLAR_CI_EGRESS 2>/dev/null || true
iptables -F SOLAR_CI_EGRESS 2>/dev/null || true
iptables -X SOLAR_CI_EGRESS 2>/dev/null || true
docker network rm "$RUNNER_NETWORK" >/dev/null 2>&1 || true
docker network rm "$EGRESS_NETWORK" >/dev/null 2>&1 || true

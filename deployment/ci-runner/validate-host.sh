#!/bin/sh
set -eu

: "${PROXY_NAME:=solar-ci-proxy}"
: "${RUNNER_NETWORK:=solar-ci-isolated}"
: "${EGRESS_NETWORK:=solar-ci-egress}"

test "$(docker network inspect --format '{{.Internal}}' "$RUNNER_NETWORK")" = true
test "$(docker network inspect --format '{{.Internal}}' "$EGRESS_NETWORK")" = false
test "$(docker inspect --format '{{.State.Running}}' "$PROXY_NAME")" = true
test "$(docker inspect --format '{{.State.Health.Status}}' "$PROXY_NAME")" = healthy
test "$(docker inspect --format '{{.Config.User}}' "$PROXY_NAME")" = "10001:10001"
test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$PROXY_NAME")" = true
test "$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$PROXY_NAME")" = null
test "$(docker inspect --format '{{.HostConfig.Privileged}}' "$PROXY_NAME")" = false
test "$(docker inspect --format '{{.AppArmorProfile}}' "$PROXY_NAME")" = solar-ci-proxy
test "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$PROXY_NAME")" = '["ALL"]'
test "$(docker inspect --format '{{.HostConfig.PidsLimit}}' "$PROXY_NAME")" -gt 0
test "$(docker inspect --format '{{.HostConfig.Memory}}' "$PROXY_NAME")" -gt 0
test "$(docker inspect --format '{{.HostConfig.NanoCpus}}' "$PROXY_NAME")" -gt 0
security_options="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$PROXY_NAME")"
printf '%s' "$security_options" | grep -q 'no-new-privileges'
! printf '%s' "$security_options" | grep -q 'seccomp=unconfined'
docker info --format '{{json .SecurityOptions}}' | grep -q 'name=seccomp,profile=builtin'
test "$(docker inspect --format '{{len .Mounts}}' "$PROXY_NAME")" = 0
networks="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$PROXY_NAME")"
test "$(printf '%s' "$networks" | wc -w)" = 2
printf '%s' "$networks" | grep -qw "$RUNNER_NETWORK"
printf '%s' "$networks" | grep -qw "$EGRESS_NETWORK"
iptables -C DOCKER-USER -j SOLAR_CI_EGRESS
egress_subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "$EGRESS_NETWORK")"
runner_subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "$RUNNER_NETWORK")"
iptables -C INPUT -s "$egress_subnet" -j REJECT
iptables -C INPUT -s "$runner_subnet" -j REJECT
apparmor_parser -Q /etc/apparmor.d/solar-ci-runner
apparmor_parser -Q /etc/apparmor.d/solar-ci-proxy

echo "host structural validation passed"

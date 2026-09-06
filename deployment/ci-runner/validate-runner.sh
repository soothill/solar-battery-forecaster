#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: validate-runner.sh CONTAINER IMAGE NETWORK" >&2
  exit 1
fi
container="$1"
image="$2"
network="$3"
expected_image_id="$(docker image inspect --format '{{.Id}}' "$image")"

test "$(docker inspect --format '{{.Image}}' "$container")" = "$expected_image_id"
test "$(docker inspect --format '{{.Config.User}}' "$container")" = "10001:10001"
test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container")" = true
test "$(docker inspect --format '{{.HostConfig.Privileged}}' "$container")" = false
test "$(docker inspect --format '{{.AppArmorProfile}}' "$container")" = solar-ci-runner
test "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$container")" = '["ALL"]'
test "$(docker inspect --format '{{.HostConfig.PidsLimit}}' "$container")" -gt 0
test "$(docker inspect --format '{{.HostConfig.Memory}}' "$container")" -gt 0
test "$(docker inspect --format '{{.HostConfig.NanoCpus}}' "$container")" -gt 0
test "$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$container")" = null
security_options="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$container")"
printf '%s' "$security_options" | grep -q 'no-new-privileges'
! printf '%s' "$security_options" | grep -q 'seccomp=unconfined'
test "$(docker inspect --format '{{len .Mounts}}' "$container")" = 1
test "$(docker inspect --format '{{(index .Mounts 0).Type}}' "$container")" = volume
test "$(docker inspect --format '{{(index .Mounts 0).Destination}}' "$container")" = /opt/actions-runner/_work
networks="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$container")"
test "$(printf '%s' "$networks" | wc -w)" = 1
printf '%s' "$networks" | grep -qw "$network"
test "$(docker network inspect --format '{{.Internal}}' "$network")" = true

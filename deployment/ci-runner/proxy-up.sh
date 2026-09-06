#!/bin/sh
set -eu

: "${PROXY_IMAGE:?set PROXY_IMAGE to an immutable image digest}"
: "${PROXY_NAME:=solar-ci-proxy}"
: "${RUNNER_NETWORK:=solar-ci-isolated}"
: "${EGRESS_NETWORK:=solar-ci-egress}"
: "${EGRESS_SUBNET:=172.30.254.0/28}"
: "${RUNNER_SUBNET:=172.30.253.0/28}"

case "$PROXY_IMAGE" in
  sha256:????????????????????????????????????????????????????????????????) ;;
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "PROXY_IMAGE must use an exact sha256 digest" >&2; exit 1 ;;
esac

docker network inspect "$RUNNER_NETWORK" >/dev/null 2>&1 \
  || docker network create --internal --subnet "$RUNNER_SUBNET" "$RUNNER_NETWORK" >/dev/null
docker network inspect "$EGRESS_NETWORK" >/dev/null 2>&1 \
  || docker network create --subnet "$EGRESS_SUBNET" "$EGRESS_NETWORK" >/dev/null
test "$(docker network inspect --format '{{.Internal}}' "$RUNNER_NETWORK")" = true
test "$(docker network inspect --format '{{.Internal}}' "$EGRESS_NETWORK")" = false
test "$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "$RUNNER_NETWORK")" = "$RUNNER_SUBNET"
test "$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "$EGRESS_NETWORK")" = "$EGRESS_SUBNET"

iptables -N SOLAR_CI_EGRESS 2>/dev/null || true
iptables -F SOLAR_CI_EGRESS
for destination in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4; do
  iptables -A SOLAR_CI_EGRESS -s "$EGRESS_SUBNET" -d "$destination" -j REJECT
done
iptables -A SOLAR_CI_EGRESS -j RETURN
iptables -C DOCKER-USER -j SOLAR_CI_EGRESS 2>/dev/null \
  || iptables -I DOCKER-USER 1 -j SOLAR_CI_EGRESS
iptables -C INPUT -s "$EGRESS_SUBNET" -j REJECT 2>/dev/null \
  || iptables -I INPUT 1 -s "$EGRESS_SUBNET" -j REJECT
iptables -C INPUT -s "$RUNNER_SUBNET" -j REJECT 2>/dev/null \
  || iptables -I INPUT 1 -s "$RUNNER_SUBNET" -j REJECT

if docker inspect "$PROXY_NAME" >/dev/null 2>&1; then
  test "$(docker inspect --format '{{.Config.Image}}' "$PROXY_NAME")" = "$PROXY_IMAGE"
  test "$(docker inspect --format '{{.State.Running}}' "$PROXY_NAME")" = true
else
  docker run --detach --name "$PROXY_NAME" --restart unless-stopped \
    --user 10001:10001 --read-only --network "$EGRESS_NETWORK" \
    --cap-drop ALL --security-opt no-new-privileges \
    --security-opt apparmor=solar-ci-proxy \
    --pids-limit 128 --memory 256m --cpus 1 \
    --tmpfs /run:rw,nosuid,nodev,noexec,size=16m,uid=10001,gid=10001 \
    --tmpfs /var/spool/squid:rw,nosuid,nodev,noexec,size=32m,uid=10001,gid=10001 \
    "$PROXY_IMAGE" >/dev/null
  docker network connect "$RUNNER_NETWORK" "$PROXY_NAME"
fi

attempt=0
until [ "$(docker inspect --format '{{.State.Health.Status}}' "$PROXY_NAME")" = healthy ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "proxy did not become healthy" >&2
    exit 1
  fi
  sleep 1
done
test "$(docker inspect --format '{{.Config.User}}' "$PROXY_NAME")" = "10001:10001"
test "$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$PROXY_NAME")" = null

#!/bin/sh
set -eu

: "${RUNNER_IMAGE:?set RUNNER_IMAGE to an immutable image digest}"
: "${RUNNER_NETWORK:=solar-ci-isolated}"
: "${RUNNER_PROXY:=solar-ci-proxy}"
: "${RUNNER_MEMORY:=4g}"
: "${RUNNER_CPUS:=2}"
: "${RUNNER_PIDS:=512}"
: "${VALIDATE_RUNNER:=/usr/local/libexec/solar-ci-runner/validate-runner.sh}"

IFS= read -r jit_config
test -n "$jit_config"

case "$RUNNER_IMAGE" in
  sha256:????????????????????????????????????????????????????????????????) ;;
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "RUNNER_IMAGE must use an exact sha256 digest" >&2; exit 1 ;;
esac

test "$(docker network inspect --format '{{.Internal}}' "$RUNNER_NETWORK")" = "true"
test "$(docker inspect --format '{{.State.Running}}' "$RUNNER_PROXY")" = "true"
test "$(docker inspect --format '{{.Config.User}}' "$RUNNER_PROXY")" = "10001:10001"
test "$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$RUNNER_PROXY")" = "null"
proxy_image="$(docker inspect --format '{{.Config.Image}}' "$RUNNER_PROXY")"
case "$proxy_image" in
  sha256:????????????????????????????????????????????????????????????????) ;;
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "proxy must use an exact sha256 image digest" >&2; exit 1 ;;
esac
proxy_network_id="$(
  docker inspect --format "{{with index .NetworkSettings.Networks \"$RUNNER_NETWORK\"}}{{.NetworkID}}{{end}}" "$RUNNER_PROXY"
)"
test -n "$proxy_network_id"
work_volume="solar-ci-work-$$"
runner_name="solar-ci-job-$$"
cleanup() {
  docker rm -f "$runner_name" >/dev/null 2>&1 || true
  docker volume rm -f "$work_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker volume create --driver local \
  --opt type=tmpfs --opt device=tmpfs \
  --opt o=uid=10001,gid=10001,mode=0700,size=2g "$work_volume" >/dev/null

docker create --name "$runner_name" --rm -i \
  --user 10001:10001 --read-only --network "$RUNNER_NETWORK" \
  --env HTTPS_PROXY="http://$RUNNER_PROXY:3128" \
  --env HTTP_PROXY="http://$RUNNER_PROXY:3128" \
  --env NO_PROXY="" \
  --mount "type=volume,source=$work_volume,target=/opt/actions-runner/_work" \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /home/runner:rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /opt/actions-runner/_diag:rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /opt/uv-cache:rw,nosuid,nodev,noexec,size=2g,uid=10001,gid=10001,mode=0700 \
  --cap-drop ALL --security-opt no-new-privileges \
  --security-opt apparmor=solar-ci-runner \
  --pids-limit "$RUNNER_PIDS" --memory "$RUNNER_MEMORY" --cpus "$RUNNER_CPUS" \
  "$RUNNER_IMAGE" >/dev/null
"$VALIDATE_RUNNER" "$runner_name" "$RUNNER_IMAGE" "$RUNNER_NETWORK"
printf '%s\n' "$jit_config" | docker start --attach --interactive "$runner_name"
unset jit_config

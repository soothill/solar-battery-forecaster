#!/bin/sh
set -eu
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "accept-host.sh must run as root" >&2
  exit 1
fi
: "${MARKER:=/var/lib/solar-ci-runner/acceptance.ok}"
: "${CHECK_ACCEPTANCE:=/usr/local/libexec/solar-ci-runner/check-acceptance.sh}"
: "${VALIDATE_RUNNER:=/usr/local/libexec/solar-ci-runner/validate-runner.sh}"

for file in /etc/solar-ci-runner/images.env /etc/solar-ci-runner/runner.env; do
  test "$(stat -c %U:%G "$file")" = root:root
  test -z "$(find "$file" -prune -perm /077 -print)"
done
set -a
. /etc/solar-ci-runner/images.env
. /etc/solar-ci-runner/runner.env
set +a
: "${RUNNER_IMAGE:?configure RUNNER_IMAGE}"
: "${PROXY_IMAGE:?configure PROXY_IMAGE}"
: "${RUNNER_NETWORK:=solar-ci-isolated}"
: "${RUNNER_PROXY:=solar-ci-proxy}"

install -d -o root -g root -m 0700 "$(dirname "$MARKER")"
rm -f "$MARKER"
systemctl start solar-ci-proxy.service
/usr/local/libexec/solar-ci-runner/validate-host.sh
systemctl start solar-ci-policy-check.service

acceptance_id="solar-ci-acceptance-$$"
work_volume="$acceptance_id-work"
cleanup() {
  docker rm -f "$acceptance_id" >/dev/null 2>&1 || true
  docker volume rm -f "$work_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker volume create --driver local \
  --opt type=tmpfs --opt device=tmpfs \
  --opt o=uid=10001,gid=10001,mode=0700,size=64m "$work_volume" >/dev/null
docker create --name "$acceptance_id" --rm \
  --label solar-ci.acceptance=true \
  --user 10001:10001 --read-only --network "$RUNNER_NETWORK" \
  --mount "type=volume,source=$work_volume,target=/opt/actions-runner/_work" \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /home/runner:rw,nosuid,nodev,noexec,size=16m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /opt/actions-runner/_diag:rw,nosuid,nodev,noexec,size=16m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /opt/uv-cache:rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=0700 \
  --cap-drop ALL --security-opt no-new-privileges \
  --security-opt apparmor=solar-ci-runner \
  --pids-limit 64 --memory 256m --cpus 1 \
  --entrypoint /opt/python/3.12/bin/python3.12 \
  "$RUNNER_IMAGE" /usr/local/libexec/solar-ci-acceptance-probe.py "$RUNNER_PROXY" >/dev/null
"$VALIDATE_RUNNER" "$acceptance_id" "$RUNNER_IMAGE" "$RUNNER_NETWORK"
docker start --attach "$acceptance_id"
cleanup
trap - EXIT HUP INT TERM
! docker inspect "$acceptance_id" >/dev/null 2>&1
! docker volume inspect "$work_volume" >/dev/null 2>&1

fingerprint="$($CHECK_ACCEPTANCE --print)"
temporary_marker="$MARKER.tmp.$$"
printf 'fingerprint=%s\naccepted_at=%s\n' "$fingerprint" "$(date -u +%FT%TZ)" > "$temporary_marker"
chmod 0600 "$temporary_marker"
mv "$temporary_marker" "$MARKER"
"$CHECK_ACCEPTANCE"
echo "runner host acceptance passed; acceptance marker installed"

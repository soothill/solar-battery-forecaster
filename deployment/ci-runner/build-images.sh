#!/bin/sh
set -eu
umask 077

if [ "$#" -ne 1 ]; then
  echo "usage: build-images.sh TOOLCHAIN_JSON" >&2
  exit 1
fi
manifest="$1"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repository_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
artifacts="$repository_root/artifacts"
python3 "$script_dir/fetch-artifacts.py" "$manifest" "$artifacts"

value() { jq -er "$1" "$manifest"; }
runner_base="$(value '.runner_base_image')"
proxy_base="$(value '.proxy_base_image')"
case "$runner_base" in
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "runner base image must use a reviewed digest" >&2; exit 1 ;;
esac
case "$proxy_base" in
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "proxy base image must use a reviewed digest" >&2; exit 1 ;;
esac

docker build --file "$script_dir/Dockerfile" --tag solar-ci-runner:candidate \
  --build-arg BASE_IMAGE="$runner_base" \
  --build-arg UBUNTU_SNAPSHOT="$(value '.ubuntu_snapshot')" \
  --build-arg ACTIONS_RUNNER_SHA256="$(value '.artifacts["actions-runner.tar.gz"].sha256')" \
  --build-arg UV_SHA256="$(value '.artifacts["uv.tar.gz"].sha256')" \
  --build-arg GH_SHA256="$(value '.artifacts["gh.tar.gz"].sha256')" \
  --build-arg GITLEAKS_SHA256="$(value '.artifacts["gitleaks.tar.gz"].sha256')" \
  --build-arg PYTHON311_SHA256="$(value '.artifacts["python311.tar.gz"].sha256')" \
  --build-arg PYTHON312_SHA256="$(value '.artifacts["python312.tar.gz"].sha256')" \
  "$repository_root"
docker build --file "$script_dir/Dockerfile.proxy" --tag solar-ci-proxy:candidate \
  --build-arg PROXY_BASE_IMAGE="$proxy_base" "$script_dir"

runner_id="$(docker image inspect --format '{{.Id}}' solar-ci-runner:candidate)"
proxy_id="$(docker image inspect --format '{{.Id}}' solar-ci-proxy:candidate)"
printf 'RUNNER_IMAGE=%s\nPROXY_IMAGE=%s\n' "$runner_id" "$proxy_id" > "$script_dir/images.env.candidate"
echo "candidate image IDs written to $script_dir/images.env.candidate"

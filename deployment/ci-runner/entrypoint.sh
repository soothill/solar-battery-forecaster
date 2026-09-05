#!/bin/sh
set -eu

cp -a /opt/uv-cache-seed/. /opt/uv-cache/
IFS= read -r jit_config
if [ -z "$jit_config" ]; then
  echo "single-use JIT configuration is required" >&2
  exit 1
fi
exec /opt/actions-runner/run.sh --jitconfig "$jit_config"

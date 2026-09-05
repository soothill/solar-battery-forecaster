#!/bin/sh
set -eu
umask 077

: "${REPOSITORY:?set REPOSITORY to owner/name}"
: "${RUNNER_GROUP_ID:?set RUNNER_GROUP_ID}"
: "${TOKEN_COMMAND:?set TOKEN_COMMAND to a root-owned executable}"
: "${RUN_ONCE:=/usr/local/libexec/solar-ci-runner/run-once.sh}"

printf '%s' "$REPOSITORY" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
printf '%s' "$RUNNER_GROUP_ID" | grep -Eq '^[0-9]+$'
test -x "$TOKEN_COMMAND"
test "$(stat -c %U "$TOKEN_COMMAND")" = root
test -z "$(find "$TOKEN_COMMAND" -prune -perm /022 -print)"
test -x "$RUN_ONCE"

access_token="$($TOKEN_COMMAND)"
case "$access_token" in
  ""|*[!A-Za-z0-9_.-]*) echo "token broker returned an invalid token" >&2; exit 1 ;;
esac
test "${#access_token}" -le 4096

runner_name="solar-ci-$(date -u +%Y%m%d%H%M%S)-$$"
cleanup_required=false
cleanup_stale_runner() {
  [ "$cleanup_required" = true ] || return 0
  cleanup_required=false
  fresh_token="$($TOKEN_COMMAND 2>/dev/null)" || {
    echo "unable to acquire a cleanup credential; inspect repository runner registrations" >&2
    return 0
  }
  case "$fresh_token" in
    ""|*[!A-Za-z0-9_.-]*)
      echo "cleanup credential was invalid; inspect repository runner registrations" >&2
      unset fresh_token
      return 0
      ;;
  esac
  runner_pages="$(
    GH_TOKEN="$fresh_token" gh api --paginate --slurp \
      "repos/$REPOSITORY/actions/runners?per_page=100" 2>/dev/null
  )" || runner_pages=""
  unset fresh_token
  runner_id="$(
    printf '%s' "$runner_pages" | jq -er --arg name "$runner_name" \
      '[.[] | .runners[]? | select(.name == $name)] | first | .id // empty' 2>/dev/null
  )" || runner_id=""
  unset runner_pages
  if [ -n "$runner_id" ]; then
    delete_token="$($TOKEN_COMMAND 2>/dev/null)" || delete_token=""
    case "$delete_token" in
      ""|*[!A-Za-z0-9_.-]*)
        echo "cleanup credential was invalid; inspect repository runner registrations" >&2
        ;;
      *)
        GH_TOKEN="$delete_token" gh api --method DELETE \
          "repos/$REPOSITORY/actions/runners/$runner_id" >/dev/null 2>&1 \
          || echo "stale runner cleanup failed; inspect repository runner registrations" >&2
        ;;
    esac
    unset delete_token runner_id
  fi
}
trap cleanup_stale_runner EXIT HUP INT TERM
jit_response="$(
  GH_TOKEN="$access_token" gh api --method POST \
    "repos/$REPOSITORY/actions/runners/generate-jitconfig" \
    --raw-field name="$runner_name" \
    --field runner_group_id="$RUNNER_GROUP_ID" \
    --raw-field work_folder="_work" \
    --raw-field 'labels[]=self-hosted' \
    --raw-field 'labels[]=linux' \
    --raw-field 'labels[]=x64' \
    --raw-field 'labels[]=ic-dev' \
    --raw-field 'labels[]=solar-public-ci' \
    --raw-field 'labels[]=isolated' \
    --raw-field 'labels[]=ephemeral' \
    --raw-field 'labels[]=no-private-net'
)"
unset access_token
cleanup_required=true
jit_config="$(printf '%s' "$jit_response" | jq -er '.encoded_jit_config | select(type == "string" and length > 0)')"
unset jit_response
if printf '%s\n' "$jit_config" | "$RUN_ONCE"; then
  cleanup_required=false
else
  status=$?
  unset jit_config
  exit "$status"
fi
unset jit_config

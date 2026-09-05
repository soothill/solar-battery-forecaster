#!/bin/sh
set -eu
umask 077

: "${REPOSITORY:?set REPOSITORY to owner/name}"
: "${TOKEN_COMMAND:?set TOKEN_COMMAND to a root-owned executable}"
printf '%s' "$REPOSITORY" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
test -x "$TOKEN_COMMAND"
test "$(stat -c %U "$TOKEN_COMMAND")" = root
test -z "$(find "$TOKEN_COMMAND" -prune -perm /022 -print)"

access_token="$($TOKEN_COMMAND)"
case "$access_token" in
  ""|*[!A-Za-z0-9_.-]*) echo "policy verifier received an invalid credential" >&2; exit 1 ;;
esac
test "${#access_token}" -le 4096
policy="$(
  GH_TOKEN="$access_token" gh api \
    "repos/$REPOSITORY/actions/permissions/fork-pr-contributor-approval" \
    --jq '.approval_policy' 2>/dev/null
)" || {
  unset access_token
  echo "unable to verify the repository fork-workflow approval policy" >&2
  exit 1
}
unset access_token
if [ "$policy" != all_external_contributors ]; then
  echo "runner disabled: fork workflows must require approval from all external contributors" >&2
  exit 1
fi
echo "fork-workflow approval policy verified"

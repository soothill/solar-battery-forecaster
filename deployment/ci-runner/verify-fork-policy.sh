#!/bin/sh
set -eu
umask 077

: "${REPOSITORY:?set REPOSITORY to owner/name}"
: "${RUNNER_GROUP_ID:?set RUNNER_GROUP_ID}"
: "${RUNNER_GROUP_NAME:=solar-public-ci}"
: "${TOKEN_COMMAND:?set TOKEN_COMMAND to a root-owned executable}"
printf '%s' "$REPOSITORY" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
printf '%s' "$RUNNER_GROUP_ID" | grep -Eq '^[0-9]+$'
test "$RUNNER_GROUP_NAME" = solar-public-ci
test -x "$TOKEN_COMMAND"
test "$(stat -c %U "$TOKEN_COMMAND")" = root
test -z "$(find "$TOKEN_COMMAND" -prune -perm /022 -print)"

access_token="$($TOKEN_COMMAND)"
case "$access_token" in
  ""|*[!A-Za-z0-9_.-]*) echo "policy verifier received an invalid credential" >&2; exit 1 ;;
esac
test "${#access_token}" -le 4096
owner="${REPOSITORY%%/*}"
required_workflow="$REPOSITORY/.github/workflows/trusted-ci.yml@refs/heads/main"
owner_type="$(
  GH_TOKEN="$access_token" gh api "users/$owner" --jq '.type' 2>/dev/null
)" || owner_type=""
if [ "$owner_type" != Organization ]; then
  unset access_token
  echo "runner disabled: repository owner must be an organization" >&2
  exit 1
fi
policy="$(
  GH_TOKEN="$access_token" gh api \
    "repos/$REPOSITORY/actions/permissions/fork-pr-contributor-approval" \
    --jq '.approval_policy' 2>/dev/null
)" || {
  unset access_token
  echo "unable to verify the repository fork-workflow approval policy" >&2
  exit 1
}
if [ "$policy" != all_external_contributors ]; then
  unset access_token
  echo "runner disabled: fork workflows must require approval from all external contributors" >&2
  exit 1
fi
group="$(
  GH_TOKEN="$access_token" gh api \
    "orgs/$owner/actions/runner-groups/$RUNNER_GROUP_ID" 2>/dev/null
)" || group=""
if ! printf '%s' "$group" | jq -e \
  --arg name "$RUNNER_GROUP_NAME" --arg workflow "$required_workflow" '
  .name == $name and
  .default == false and
  .visibility == "selected" and
  .restricted_to_workflows == true and
  .selected_workflows == [$workflow]
' >/dev/null; then
  unset access_token group
  echo "runner disabled: organization runner group restrictions are invalid" >&2
  exit 1
fi
repositories="$(
  GH_TOKEN="$access_token" gh api --paginate --slurp \
    "orgs/$owner/actions/runner-groups/$RUNNER_GROUP_ID/repositories?per_page=100" \
    2>/dev/null
)" || repositories=""
unset access_token group
if ! printf '%s' "$repositories" | jq -e --arg repository "$REPOSITORY" '
  [.[] | .repositories[]?.full_name] == [$repository]
' >/dev/null; then
  unset repositories
  echo "runner disabled: organization runner group repository selection is invalid" >&2
  exit 1
fi
unset repositories
echo "fork and organization runner-group policies verified"

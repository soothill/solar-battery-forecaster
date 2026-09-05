#!/bin/sh
set -eu
umask 077

: "${GITHUB_APP_ID:?set GITHUB_APP_ID}"
: "${GITHUB_INSTALLATION_ID:?set GITHUB_INSTALLATION_ID}"
: "${CREDENTIALS_DIRECTORY:?systemd credential directory is unavailable}"
private_key="$CREDENTIALS_DIRECTORY/github-app-key"
test -r "$private_key"
printf '%s' "$GITHUB_APP_ID" | grep -Eq '^[0-9]+$'
printf '%s' "$GITHUB_INSTALLATION_ID" | grep -Eq '^[0-9]+$'

base64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}
issued_at="$(($(date +%s) - 60))"
expires_at="$((issued_at + 540))"
header="$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | base64url)"
payload="$(printf '{"iat":%s,"exp":%s,"iss":"%s"}' "$issued_at" "$expires_at" "$GITHUB_APP_ID" | base64url)"
unsigned="$header.$payload"
signature="$(printf '%s' "$unsigned" | openssl dgst -sha256 -sign "$private_key" | base64url)"
jwt="$unsigned.$signature"

GH_TOKEN="$jwt" gh api --method POST \
  "app/installations/$GITHUB_INSTALLATION_ID/access_tokens" --jq '.token'
unset jwt unsigned signature

#!/bin/bash
# Sets up one GitHub repository to publish to the web host. Run it logged in to gh with rights to
# write the repository's environments and secrets.
#
#   PUBLISH_HOST=<host> PUBLISH_USER=<account> PUBLISH_HOST_KEY_FP=SHA256:... \
#     tools/add-publisher.sh <owner>/<repo>
#
# It:
#   1. checks the host's ED25519 key against PUBLISH_HOST_KEY_FP (take it from the host's console or
#      your hosting panel, not from a scan over the network);
#   2. creates (or updates) the repository's `ci-publish` environment, deployable from main only;
#   3. generates a new publishing key and stores it, with the host details, as environment secrets;
#   4. prints the one line to add to ~/.ssh/authorized_keys on the host.
# Private keys exist only in a temporary folder that is deleted on exit, and are never printed.
# Running it again replaces the keys: delete the repository's old line on the host afterwards.
set -euo pipefail

HOST=${PUBLISH_HOST:?set PUBLISH_HOST}
ACCOUNT=${PUBLISH_USER:?set PUBLISH_USER}
HOST_KEY_FP=${PUBLISH_HOST_KEY_FP:?set PUBLISH_HOST_KEY_FP}
ENV_NAME=ci-publish

die() { echo "add-publisher: $*" >&2; exit 1; }

repo=${1:-}
[[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "usage: add-publisher.sh <owner/repo>"
name=${repo#*/}
for tool in gh ssh-keygen ssh-keyscan; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
chmod 700 "$tmp"

ssh-keyscan -T 10 -t ed25519 "$HOST" 2>/dev/null | grep -v '^#' >"$tmp/known_hosts" || true
[ -s "$tmp/known_hosts" ] || die "could not read the host key of $HOST"
fp=$(ssh-keygen -lf "$tmp/known_hosts" | awk '{print $2}')
[ "$fp" = "$HOST_KEY_FP" ] || die "host key is $fp, not the recorded $HOST_KEY_FP; stopping"

visibility=$(gh repo view "$repo" --json visibility --jq .visibility) || die "can't read $repo"

gh api -X PUT "repos/$repo/environments/$ENV_NAME" --input - >/dev/null <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
if ! gh api "repos/$repo/environments/$ENV_NAME/deployment-branch-policies" \
  --jq '.branch_policies[].name' | grep -qx main; then
  gh api -X POST "repos/$repo/environments/$ENV_NAME/deployment-branch-policies" \
    -f name=main -f type=branch >/dev/null
fi

ssh-keygen -q -t ed25519 -N '' -C "gha-$name-publish" -f "$tmp/publish"
gh secret set PUBLISH_SSH_KEY --repo "$repo" --env "$ENV_NAME" <"$tmp/publish"
gh secret set PUBLISH_KNOWN_HOSTS --repo "$repo" --env "$ENV_NAME" <"$tmp/known_hosts"
gh secret set PUBLISH_HOST --repo "$repo" --env "$ENV_NAME" --body "$HOST"
gh secret set PUBLISH_USER --repo "$repo" --env "$ENV_NAME" --body "$ACCOUNT"

echo "Done: $repo ($visibility), environment $ENV_NAME limited to main."
echo "Add this line to ~/.ssh/authorized_keys on $HOST:"
echo
printf 'command="/usr/bin/rrsync -wo -no-lock /home/%s/ci-drop",restrict %s\n' "$ACCOUNT" "$(cat "$tmp/publish.pub")"

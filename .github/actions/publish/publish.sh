#!/bin/bash
# Uploads $SOURCE to the publishing host's drop for $CHANNEL, then writes the .ready marker that tells
# promote.sh the upload is complete. Needs PUBLISH_SSH_KEY, PUBLISH_KNOWN_HOSTS, PUBLISH_HOST and
# PUBLISH_USER in the environment (from a protected GitHub environment's secrets).
set -euo pipefail

die() { echo "publish: $*" >&2; exit 1; }

: "${CHANNEL:?}" "${SOURCE:?}" "${DEST:?}"
: "${PUBLISH_SSH_KEY:?set the PUBLISH_SSH_KEY secret}" "${PUBLISH_KNOWN_HOSTS:?set the PUBLISH_KNOWN_HOSTS secret}"
: "${PUBLISH_HOST:?set the PUBLISH_HOST secret}" "${PUBLISH_USER:?set the PUBLISH_USER secret}"

case $CHANNEL in reports | mirror | pkg | docs | landing) ;; *) die "unknown channel '$CHANNEL'" ;; esac
[[ $DEST =~ ^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$ ]] || die "bad dest '$DEST'"
[ -d "$SOURCE" ] || die "source '$SOURCE' is not a directory"
[ -n "$(find "$SOURCE" -type f -print -quit)" ] || die "source '$SOURCE' is empty"

work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/publish.XXXXXX")
trap 'rm -rf "$work"' EXIT
(umask 077 && printf '%s\n' "$PUBLISH_SSH_KEY" >"$work/key" && printf '%s\n' "$PUBLISH_KNOWN_HOSTS" >"$work/known_hosts")

ssh_cmd="ssh -i $work/key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$work/known_hosts"
id="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
remote="$PUBLISH_USER@$PUBLISH_HOST"
# Secrets are masked in the log, but an ssh or rsync error can print the host's address instead of its
# name. Mask the addresses too.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  getent ahosts "$PUBLISH_HOST" | awk '{print $1}' | sort -u | while read -r addr; do echo "::add-mask::$addr"; done
fi

# rrsync without -no-lock lets one session per key run at a time, and refuses the rest with
# "Another instance of rrsync is already accessing this directory". Wait for it (a large mirror
# file can hold it for many minutes) instead of failing; any other error fails at once.
send() {
  local attempt err
  for attempt in $(seq 1 60); do
    if err=$(rsync "$@" 2>&1); then
      [ -z "$err" ] || printf '%s\n' "$err"
      return 0
    fi
    case $err in
      *"Another instance of rrsync"*) echo "publish: host busy with another upload, retrying in 30 s ($attempt/60)" ;;
      *) printf '%s\n' "$err" >&2; return 1 ;;
    esac
    sleep 30
  done
  die "host stayed busy for 30 minutes"
}

# Paths are relative to ~/ci-drop: the key's forced rrsync command roots them there.
send -rt --chmod=D755,F644 --info=stats1 -e "$ssh_cmd" "$SOURCE/" "$remote:$CHANNEL/$id/"
printf '%s\n' "$DEST" >"$work/marker"
send -t --chmod=F644 -e "$ssh_cmd" "$work/marker" "$remote:$CHANNEL/$id.ready"

echo "publish: $CHANNEL/$id -> $DEST (live after the next promote run, within ~5 minutes)"

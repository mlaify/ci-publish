#!/bin/bash
# Installs a pinned gitleaks, scans $SCAN_PATH's files and history, renders the report into
# $LEAK_OUT (default $RUNNER_TEMP/leak-report). Prints only the one-line summary: findings never
# reach the log. LEAK_RULES, if set, is a gitleaks config that extends the default rules.
set -euo pipefail

GITLEAKS_VERSION=8.30.1
GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb

tmp="${RUNNER_TEMP:?}"
bin="$tmp/leak-bin"
out="${LEAK_OUT:-$tmp/leak-report}"
work=$(mktemp -d "$tmp/leak.XXXXXX")
trap 'rm -rf "$work"' EXIT

if [ ! -x "$bin/gitleaks" ]; then
  mkdir -p "$bin"
  curl -fsSL -o "$work/gitleaks.tgz" \
    "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
  echo "$GITLEAKS_SHA256  $work/gitleaks.tgz" | sha256sum -c --quiet -
  tar -xzf "$work/gitleaks.tgz" -C "$bin" gitleaks
fi

config=()
if [ -n "${LEAK_RULES:-}" ]; then
  (umask 077 && printf '%s\n' "$LEAK_RULES" >"$work/rules.toml")
  config=(--config "$work/rules.toml")
fi
common=(--redact --no-banner --log-level error --exit-code 0 --report-format json "${config[@]}")
"$bin/gitleaks" dir "$SCAN_PATH" "${common[@]}" --report-path "$work/files.json"
"$bin/gitleaks" git "$SCAN_PATH" "${common[@]}" --report-path "$work/history.json"

rm -rf "$out"
mkdir -p "$out"
REPORT_SHA=${REPORT_SHA:-$(git -C "$SCAN_PATH" rev-parse HEAD)} \
REPORT_REF=${REPORT_REF:-$(git -C "$SCAN_PATH" rev-parse --abbrev-ref HEAD)} \
  python3 "$GITHUB_ACTION_PATH/render_leaks.py" "$work/files.json" "$work/history.json" "$out"
echo "dir=$out" >>"${GITHUB_OUTPUT:-/dev/null}"
cat "$out/summary.txt"

#!/bin/bash
# Installs pinned scanners, scans $SCAN_PATH, renders the report. Tool versions are pinned and
# at least two weeks old when chosen; Trivy is checked against its published SHA-256.
set -euo pipefail

TRIVY_VERSION=0.75.0
TRIVY_SHA256=c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f
SEMGREP_VERSION=1.180.0

tmp="${RUNNER_TEMP:?}"
bin="$tmp/scan-bin"
out="$tmp/security-report"
# A job that scans several checkouts in turn installs the tools once and starts each report afresh.
rm -rf "$out"
mkdir -p "$bin" "$out/sarif"

if [ ! -x "$bin/trivy" ]; then
  curl -fsSL -o "$tmp/trivy.tgz" \
    "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
  echo "$TRIVY_SHA256  $tmp/trivy.tgz" | sha256sum -c --quiet -
  tar -xzf "$tmp/trivy.tgz" -C "$bin" trivy
fi

if [ ! -x "$tmp/semgrep-venv/bin/semgrep" ]; then
  python3 -m venv "$tmp/semgrep-venv"
  "$tmp/semgrep-venv/bin/pip" install --quiet --disable-pip-version-check "semgrep==$SEMGREP_VERSION"
fi

"$bin/trivy" fs --quiet --scanners vuln,secret,misconfig --format json -o "$out/trivy.json" "$SCAN_PATH"
"$bin/trivy" convert --quiet --format sarif -o "$out/sarif/trivy.sarif" "$out/trivy.json"
"$bin/trivy" fs --quiet --format cyclonedx -o "$out/sbom.cdx.json" "$SCAN_PATH"

read -r -a configs <<<"$SEMGREP_CONFIG"
config_args=()
for c in "${configs[@]}"; do config_args+=(--config "$c"); done
"$tmp/semgrep-venv/bin/semgrep" scan --metrics=off --quiet "${config_args[@]}" \
  --sarif --output "$out/sarif/semgrep.sarif" "$SCAN_PATH"

python3 "$GITHUB_ACTION_PATH/render_report.py" "$out"
echo "dir=$out" >>"$GITHUB_OUTPUT"
cat "$out/summary.txt"

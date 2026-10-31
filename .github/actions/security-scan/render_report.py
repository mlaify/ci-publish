"""Renders index.html and summary.txt for one security report directory.

Reads trivy.json and sarif/semgrep.sarif written by scan.sh. Secret findings show the rule,
file and line only, never the matched text.
"""
import datetime
import html
import json
import os
import sys
from collections import Counter
from pathlib import Path

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
MAX_ROWS = 500


def esc(value):
    return html.escape(str(value if value is not None else ""))


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def trivy_rows(report):
    vulns, secrets, misconfigs = [], [], []
    for result in report.get("Results") or []:
        target = result.get("Target", "")
        for v in result.get("Vulnerabilities") or []:
            vulns.append((v.get("Severity", "UNKNOWN"), v.get("VulnerabilityID"), v.get("PkgName"),
                          v.get("InstalledVersion"), v.get("FixedVersion") or "-", target))
        for s in result.get("Secrets") or []:
            secrets.append((s.get("Severity", "UNKNOWN"), s.get("RuleID"), target, s.get("StartLine")))
        for m in result.get("Misconfigurations") or []:
            if m.get("Status") == "FAIL":
                misconfigs.append((m.get("Severity", "UNKNOWN"), m.get("ID"), m.get("Title"), target))
    order = {s: i for i, s in enumerate(SEVERITIES)}
    for rows in (vulns, secrets, misconfigs):
        rows.sort(key=lambda r: order.get(r[0], len(order)))
    return vulns, secrets, misconfigs


def semgrep_rows(sarif):
    rows = []
    for run in sarif.get("runs") or []:
        for r in run.get("results") or []:
            loc = ((r.get("locations") or [{}])[0].get("physicalLocation") or {})
            rows.append((r.get("level", "warning"), r.get("ruleId"),
                         (loc.get("artifactLocation") or {}).get("uri"),
                         (loc.get("region") or {}).get("startLine")))
    rows.sort(key=lambda r: {"error": 0, "warning": 1, "note": 2}.get(r[0], 3))
    return rows


def table(title, headers, rows):
    out = [f"<h2>{esc(title)} ({len(rows)})</h2>"]
    if not rows:
        return out[0] + "<p>None.</p>"
    out.append("<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>")
    for row in rows[:MAX_ROWS]:
        out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>")
    out.append("</table>")
    if len(rows) > MAX_ROWS:
        out.append(f"<p>First {MAX_ROWS} shown; the full list is in the SARIF files.</p>")
    return "\n".join(out)


def main(report_dir):
    d = Path(report_dir)
    vulns, secrets, misconfigs = trivy_rows(load(d / "trivy.json", {}))
    semgrep = semgrep_rows(load(d / "sarif" / "semgrep.sarif", {}))

    by_sev = Counter(r[0] for r in vulns)
    summary = ("vulns " + " ".join(f"{s[0]}{by_sev[s]}" for s in SEVERITIES[:4])
               + f"; secrets {len(secrets)}; misconfig {len(misconfigs)}; semgrep {len(semgrep)}")
    (d / "summary.txt").write_text(summary + "\n")

    # REPORT_* describe the code scanned when it isn't the workflow's own repository (scan-all.yml).
    repo = os.environ.get("REPORT_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("REPORT_SHA") or os.environ.get("GITHUB_SHA", "")
    ref = os.environ.get("REPORT_REF") or os.environ.get("GITHUB_REF_NAME", "")
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
    meta = {"repository": repo, "sha": sha, "ref": ref,
            "run": run_url, "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "summary": summary}
    (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    page = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{esc(repo)} {esc(sha[:12])}</title>",
        "<style>body{font:14px system-ui,sans-serif;margin:2em;max-width:80em}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:3px 6px;text-align:left}"
        "th{background:#eee}</style>",
        f"<h1>{esc(repo)}</h1>",
        f"<p>Commit <code>{esc(sha)}</code> on <code>{esc(meta['ref'])}</code>, {esc(meta['generated'])}. "
        f"<a href='{esc(run_url)}'>Workflow run</a> | <a href='../'>all commits</a></p>",
        f"<p><b>{esc(summary)}</b></p>",
        "<p>Files: <a href='sbom.cdx.json'>SBOM (CycloneDX)</a> | <a href='trivy.json'>trivy.json</a> | "
        "<a href='sarif/trivy.sarif'>trivy.sarif</a> | <a href='sarif/semgrep.sarif'>semgrep.sarif</a></p>",
        table("Vulnerabilities", ["severity", "id", "package", "installed", "fixed", "target"], vulns),
        table("Secrets", ["severity", "rule", "file", "line"], secrets),
        table("Misconfigurations", ["severity", "id", "title", "target"], misconfigs),
        table("Semgrep", ["level", "rule", "file", "line"], semgrep),
    ]
    (d / "index.html").write_text("\n".join(page) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])

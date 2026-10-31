"""Renders gitleaks results into a report directory: index.html, summary.txt, meta.json.

  render_leaks.py <files.json> <history.json> <out dir>

files.json is the scan of the current files, history.json the scan of every commit. Each finding is
shown once per (rule, file): whether it is still in the current files, how many commits carry it, and
the first and last of them. Values stay redacted, and commit authors, emails and messages are dropped.
"""
import datetime
import html
import json
import os
import sys
from pathlib import Path

LEVELS = ["high", "medium", "low"]
STYLE = (":root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--accent:#0b6bcb;"
         "--bad:#b42318}@media (prefers-color-scheme:dark){:root{--bg:#151517;--fg:#e8e8ea;--muted:#9a9aa0;"
         "--line:#2c2c30;--accent:#6cb2ff;--bad:#ff8a80}}body{margin:0;background:var(--bg);color:var(--fg);"
         "font:15px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif}main{max-width:1100px;margin:0 auto;"
         "padding:40px 16px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px}"
         "p.lede{color:var(--muted)}a{color:var(--accent)}.table{overflow-x:auto}table{border-collapse:collapse;"
         "width:100%;font-size:14px}th,td{text-align:left;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);"
         "vertical-align:top}th{color:var(--muted);font-weight:500}code{font-size:13px}.bad{color:var(--bad)}")


def load(path):
    try:
        return json.loads(Path(path).read_text() or "[]") or []
    except FileNotFoundError:
        return []


def level(finding):
    tags = [t.lower() for t in finding.get("Tags") or []]
    return next((lv for lv in LEVELS if lv in tags), "high")  # gitleaks' own secret rules carry no level


def main():
    files, history, out = load(sys.argv[1]), load(sys.argv[2]), Path(sys.argv[3])
    repo = os.environ.get("REPORT_REPOSITORY", "")
    sha = os.environ.get("REPORT_SHA", "")
    ref = os.environ.get("REPORT_REF", "")
    public = os.environ.get("REPORT_VISIBILITY", "private").lower() == "public"
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
               f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")

    # The files scan reports paths as given on the command line; history paths are repository-relative.
    prefix = os.environ.get("SCAN_PATH", "").rstrip("/") + "/"
    for f in files:
        if prefix != "/" and f.get("File", "").startswith(prefix):
            f["File"] = f["File"][len(prefix):]
        elif f.get("File", "").startswith("./"):
            f["File"] = f["File"][2:]

    rows = {}
    for f in history:
        key = (f.get("RuleID", ""), f.get("File", ""))
        r = rows.setdefault(key, {"rule": key[0], "file": key[1], "desc": f.get("Description", ""),
                                  "level": level(f), "lines": set(), "commits": {}, "current": False})
        r["lines"].add(f.get("StartLine"))
        r["commits"][f.get("Commit", "")] = f.get("Date", "")
    for f in files:
        key = (f.get("RuleID", ""), f.get("File", ""))
        r = rows.setdefault(key, {"rule": key[0], "file": key[1], "desc": f.get("Description", ""),
                                  "level": level(f), "lines": set(), "commits": {}, "current": False})
        r["current"] = True
        r["lines"].add(f.get("StartLine"))

    counts = {lv: sum(1 for r in rows.values() if r["level"] == lv) for lv in LEVELS}
    current = sum(1 for r in rows.values() if r["current"])
    summary = (f"{'PUBLIC ' if public else ''}leaks {len(rows)}: high {counts['high']}, medium {counts['medium']}, "
               f"low {counts['low']}; {current} in current files")
    generated = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    (out / "summary.txt").write_text(summary + "\n")
    (out / "meta.json").write_text(json.dumps({"repository": repo, "sha": sha, "ref": ref, "run": run_url,
                                               "public": public, "generated": generated,
                                               "summary": summary}, indent=2) + "\n")

    e = html.escape
    order = sorted(rows.values(), key=lambda r: (LEVELS.index(r["level"]), not r["current"], r["rule"], r["file"]))

    def table(title, items, note):
        if not items:
            return f"<h2>{e(title)}</h2><p>None.</p>"
        body = [f"<h2>{e(title)} ({len(items)})</h2><p class=lede>{e(note)}</p><div class=table><table>",
                "<tr><th>level</th><th>rule</th><th>file</th><th>lines</th><th>commits</th><th>first / last seen</th></tr>"]
        for r in items:
            dates = sorted(d for d in r["commits"].values() if d)
            seen = f"{dates[0][:10]} / {dates[-1][:10]}" if dates else ""
            lines = ", ".join(str(n) for n in sorted(x for x in r["lines"] if x is not None)[:8])
            body.append(f"<tr><td>{e(r['level'])}</td><td title='{e(r['desc'])}'>{e(r['rule'])}</td>"
                        f"<td><code>{e(r['file'])}</code></td><td>{e(lines)}</td><td>{len(r['commits'])}</td>"
                        f"<td>{e(seen)}</td></tr>")
        body.append("</table></div>")
        return "\n".join(body)

    exposure = ("<p class=bad><b>This repository is public:</b> anything below, including history-only findings, "
                "is readable by anyone. Removing a value from the current files does not remove it from history: "
                "rotate secrets, and rewrite history for private details.</p>") if public else ""
    page = [
        "<!doctype html><html lang=en><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width, initial-scale=1'>",
        f"<title>{e(repo)} leak scan</title><style>{STYLE}</style><main>",
        f"<h1>{e(repo)}</h1>",
        f"<p class=lede>Leak scan of commit <code>{e(sha[:12])}</code> on <code>{e(ref)}</code>, {e(generated)}. "
        f"<a href='{e(run_url)}'>Workflow run</a> | <a href='../'>all commits</a></p>",
        f"<p><b>{e(summary)}</b></p>",
        exposure,
        table("In the current files", [r for r in order if r["current"]],
              "Present at this commit. Values are redacted."),
        table("Only in history", [r for r in order if not r["current"]],
              "Gone from the current files but still in earlier commits."),
        "</main>",
    ]
    (out / "index.html").write_text("\n".join(page) + "\n")


if __name__ == "__main__":
    main()

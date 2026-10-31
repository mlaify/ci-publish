"""Builds the root landing pages of mirror.<domain> and pkg.<domain> for landing.yml.

  landing.py mirror <manifest.toml> <out dir>
  landing.py pkg <pip index base URL> <out dir>

Each writes <out dir>/index.html, which the landing channel moves to the site's root. The
reports site's root page is written by promote.sh on the host, which knows every repository.
"""
import html
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STYLE = """
:root { color-scheme: light dark; --bg: #fbfbfa; --fg: #1d1d1f; --muted: #6b6b70; --line: #e3e3e0;
  --accent: #0b6bcb; --code: #f0f0ed; }
@media (prefers-color-scheme: dark) { :root { --bg: #151517; --fg: #e8e8ea; --muted: #9a9aa0;
  --line: #2c2c30; --accent: #6cb2ff; --code: #202024; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 40px 16px 64px; }
h1 { font-size: 28px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin: 36px 0 8px; }
p.lede { color: var(--muted); margin: 0 0 24px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, pre { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--code);
  border-radius: 6px; }
code { padding: 1px 5px; }
pre { padding: 12px 14px; overflow-x: auto; }
.table { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 7px 10px 7px 0; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
td.hash { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
footer { margin-top: 48px; color: var(--muted); font-size: 13px; }
"""

# One line per top-level mirror directory; unknown ones are listed without a note.
MIRROR_SECTIONS = {
    "ubuntu": "Ubuntu Server ISOs, checked against Ubuntu's signed SHA256SUMS.",
    "ubuntu-cloud": "Ubuntu cloud images, checked against the signed SHA256SUMS of the release.",
    "fedora": "Fedora Workstation and Silverblue, latest stable, checked against Fedora's signed CHECKSUM.",
    "debian": "Debian netinst, checked against Debian's signed SHA256SUMS.",
    "alpine": "Alpine virt ISOs, each signed by the Alpine release key.",
    "kali": "Kali installer and QEMU image, checked against Kali's signed SHA256SUMS.",
    "remnux": "REMnux malware-analysis VM. Hashes from the REMnux docs page, after a 14-day wait.",
    "tools": "Reverse-engineering and analysis tools. GitHub release files are checked against "
             "GitHub's SHA-256 after a 14-day wait; files with no published hash are pinned on first sight.",
    "wordlists": "Wordlists, pinned on first sight.",
    "targets": "Intentionally vulnerable VMs. Isolated lab networks only.",
}


def page(title, body):
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<meta name=robots content='noindex, nofollow'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><main>{body}<footer>Published by ci-publish.</footer></main></body></html>\n")


def mirror(manifest, out):
    entries = tomllib.loads(Path(manifest).read_text()).get("file", [])
    sections = {}
    for e in entries:
        parts = e["path"].split("/")
        sections.setdefault(parts[0], []).append((parts[1:-1], parts[-1], e))
    e = html.escape
    # The page is served from the mirror itself; MIRROR_URL (a secret in CI) only fills in the example.
    site = os.environ.get("MIRROR_URL") or "https://mirror.example.org"
    body = ["<h1>Lab mirror</h1>",
            f"<p class=lede>{len(entries)} files: OS images and tools for the lab, each pinned to a "
            "SHA-256 in the ci-publish manifest. Open from allowed networks, or with the mirror login.</p>",
            "<h2>Fetch and verify</h2>",
            "<p>Every file has a <code>.sha256</code> next to it, but that file comes from this server. "
            "To check a download against the hash in git instead, use <code>tools/mirror-get.py</code> "
            "from a ci-publish checkout:</p>",
            f"<pre>MIRROR_URL={e(site)} tools/mirror-get.py "
            "ubuntu/26.04.1/ubuntu-26.04.1-live-server-amd64.iso</pre>"]
    for name in sorted(sections):
        body.append(f"<h2 id='{e(name)}'>{e(name)}</h2>")
        if name in MIRROR_SECTIONS:
            body.append(f"<p>{e(MIRROR_SECTIONS[name])}</p>")
        body.append("<div class=table><table><tr><th>release</th><th>file</th><th>SHA-256</th><th>upstream</th></tr>")
        for middle, fname, ent in sorted(sections[name], key=lambda x: x[2]["path"]):
            path = urllib.parse.quote(ent["path"])
            sha = ent["sha256"].lower()
            host = urllib.parse.urlsplit(ent["url"]).netloc
            body.append(f"<tr><td>{e(' / '.join(middle))}</td><td><a href='{path}'>{e(fname)}</a></td>"
                        f"<td class=hash><a href='{path}.sha256' title='{sha}'>{sha[:16]}…</a></td>"
                        f"<td><a href='{e(ent['url'])}' rel=noreferrer>{e(host)}</a></td></tr>")
        body.append("</table></div>")
    write(out, page("Lab mirror", "\n".join(body)))


def version_key(v):
    return [(0, int(p)) if p.isdigit() else (-1, p) for p in re.split(r"[.+-]", v)]


def pkg(base_url, out):
    req = urllib.request.Request(f"{base_url.rstrip('/')}/state.json",
                                 headers={"User-Agent": "ci-publish-landing"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            state = json.loads(r.read())
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
        state = {}
    e = html.escape
    rows = []
    for project in sorted(state):
        versions = set()
        for fname in state[project]:
            if fname.endswith(".whl"):
                versions.add(fname.split("-")[1])  # name-version-python-abi-platform.whl
            else:
                versions.add(re.sub(r"\.(tar\.gz|zip)$", "", fname).rsplit("-", 1)[1])
        latest = max(versions, key=version_key) if versions else "?"
        rows.append(f"<tr><td><a href='pypi/simple/{e(project)}/'>{e(project)}</a></td>"
                    f"<td>{e(latest)}</td><td>{len(versions)}</td></tr>")
    simple = f"{base_url.rstrip('/')}/simple/"
    body = ["<h1>Packages</h1>",
            "<p class=lede>Python packages from mlaify's GitHub releases. New releases appear here within six hours.</p>",
            "<h2>Install with uv</h2>",
            "<p>uv looks in this index first and takes each of these packages only from here. "
            "Their dependencies still come from PyPI.</p>",
            f"<pre>uv tool install attackmap --index mlaify={e(simple)} --index-strategy first-index</pre>",
            "<p>In a project's <code>pyproject.toml</code>:</p>",
            f"<pre>[[tool.uv.index]]\nname = \"mlaify\"\nurl = \"{e(simple)}\"\nexplicit = true\n\n"
            "[tool.uv.sources]\nattackmap = { index = \"mlaify\" }</pre>",
            "<p>Avoid <code>pip --extra-index-url</code>. pip takes the highest version from either index, "
            "so a package someone else registers on PyPI under one of these names would win.</p>",
            f"<h2>Projects</h2><p>Simple index (PEP 503): <a href='pypi/simple/'>{e(simple)}</a></p>",
            "<div class=table><table><tr><th>project</th><th>latest</th><th>versions</th></tr>",
            *(rows or ["<tr><td colspan=3>Nothing published yet.</td></tr>"]),
            "</table></div>"]
    write(out, page("Packages", "\n".join(body)))


def write(out, text):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(text)


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in ("mirror", "pkg"):
        sys.exit(__doc__)
    {"mirror": mirror, "pkg": pkg}[sys.argv[1]](sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()

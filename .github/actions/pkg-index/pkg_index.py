"""Builds the staging tree for an APT or PEP 503 (pip) repository on the publishing host.

The live repository is the state: its current index is fetched over HTTPS, the new packages
are merged in, and only the new packages plus the rewritten indexes are staged. promote.sh
moves the packages into place before the indexes.

  apt:  pool/main/<letter>/<package>/<file>.deb, dists/<suite>/main/binary-<arch>/Packages{,.gz},
        dists/<suite>/Release (+ InRelease, Release.gpg when signed), key.asc
  pypi: packages/<file>, simple/index.html, simple/<project>/index.html, state.json
"""
import argparse
import datetime
import gzip
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def fetch(url):
    """Returns the body, or None when the file doesn't exist yet (first publish)."""
    try:
        # Some CDNs refuse Python's default user agent (HTTP 403).
        req = urllib.request.Request(url, headers={"User-Agent": "ci-publish-pkg-index"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- APT -------------------------------------------------------------------------------

def stanzas(text):
    return [s.strip("\n") for s in re.split(r"\n\s*\n", text) if s.strip()]


def field(stanza, name):
    m = re.search(rf"^{name}:\s*(.+)$", stanza, re.M)
    return m.group(1).strip() if m else ""


def apt(args):
    base = args.base_url.rstrip("/")
    staging = Path(args.staging)
    debs = sorted(Path(args.packages).glob("*.deb"))
    if not debs:
        sys.exit("no .deb files in " + args.packages)

    for deb in debs:
        name = subprocess.run(["dpkg-deb", "-f", str(deb), "Package"], check=True,
                              capture_output=True, text=True).stdout.strip()
        dest = staging / "pool" / "main" / name[0] / name / deb.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(deb, dest)
    scanned = subprocess.run(["dpkg-scanpackages", "--multiversion", "pool"], cwd=staging,
                             check=True, capture_output=True, text=True).stdout
    new = stanzas(scanned)

    dist = f"dists/{args.suite}"
    old_release = (fetch(f"{base}/{dist}/Release") or b"").decode()
    archs = set(args.architectures.split()) | set(field(old_release, "Architectures").split())
    archs |= {field(s, "Architecture") for s in new} - {"all"}

    index_files = []
    for arch in sorted(archs):
        rel = f"main/binary-{arch}/Packages"
        current = {}
        for s in stanzas((fetch(f"{base}/{dist}/{rel}") or b"").decode()):
            current[(field(s, "Package"), field(s, "Version"), field(s, "Architecture"))] = s
        for s in new:
            if field(s, "Architecture") in (arch, "all"):
                current[(field(s, "Package"), field(s, "Version"), field(s, "Architecture"))] = s
        body = ("\n\n".join(current[k] for k in sorted(current)) + "\n").encode() if current else b""
        out = staging / dist / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        (out.parent / "Packages.gz").write_bytes(gzip.compress(body, mtime=0))
        index_files += [rel, rel + ".gz"]

    date = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
    lines = [f"Origin: {args.origin}", f"Label: {args.origin}", f"Suite: {args.suite}",
             f"Codename: {args.suite}", f"Date: {date}", f"Architectures: {' '.join(sorted(archs))}",
             "Components: main", "SHA256:"]
    for rel in index_files:
        p = staging / dist / rel
        lines.append(f" {sha256(p)} {p.stat().st_size:>16} {rel}")
    release = staging / dist / "Release"
    release.write_text("\n".join(lines) + "\n")

    if args.gpg_key:
        gpg = ["gpg", "--batch", "--yes", "--local-user", args.gpg_key]
        subprocess.run(gpg + ["--clearsign", "-o", str(release.with_name("InRelease")), str(release)], check=True)
        subprocess.run(gpg + ["--armor", "--detach-sign", "-o", str(release.with_name("Release.gpg")), str(release)],
                       check=True)
        key = subprocess.run(["gpg", "--batch", "--armor", "--export", args.gpg_key], check=True,
                             capture_output=True).stdout
        (staging / "key.asc").write_bytes(key)
    else:
        print("warning: Release is unsigned; clients need [trusted=yes]", file=sys.stderr)


# --- pip (PEP 503 simple index) ---------------------------------------------------------

def project_of(filename):
    if filename.endswith(".whl"):
        name = filename.split("-")[0]
    else:
        name = re.sub(r"\.(tar\.gz|zip)$", "", filename).rsplit("-", 1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def pypi(args):
    base = args.base_url.rstrip("/")
    staging = Path(args.staging)
    files = sorted(p for p in Path(args.packages).iterdir()
                   if p.name.endswith((".whl", ".tar.gz", ".zip")))
    if not files:
        sys.exit("no .whl/.tar.gz/.zip files in " + args.packages)

    state = json.loads(fetch(f"{base}/state.json") or b"{}")
    (staging / "packages").mkdir(parents=True)
    for f in files:
        shutil.copy2(f, staging / "packages" / f.name)
        state.setdefault(project_of(f.name), {})[f.name] = sha256(f)

    simple = staging / "simple"
    simple.mkdir()
    head = "<!DOCTYPE html><html><head><meta name='pypi:repository-version' content='1.0'>"
    links = "".join(f"<a href='{html.escape(p)}/'>{html.escape(p)}</a><br>" for p in sorted(state))
    (simple / "index.html").write_text(f"{head}<title>Simple index</title></head><body>{links}</body></html>\n")
    for project, entries in state.items():
        (simple / project).mkdir()
        links = "".join(
            f"<a href='../../packages/{html.escape(n)}#sha256={h}'>{html.escape(n)}</a><br>"
            for n, h in sorted(entries.items()))
        (simple / project / "index.html").write_text(
            f"{head}<title>Links for {html.escape(project)}</title></head><body>{links}</body></html>\n")
    (staging / "state.json").write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["apt", "pypi"])
    ap.add_argument("--packages", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--staging", required=True)
    ap.add_argument("--gpg-key", default="")
    ap.add_argument("--suite", default="stable")
    ap.add_argument("--architectures", default="amd64 arm64")
    ap.add_argument("--origin", default="mlaify")
    args = ap.parse_args()
    {"apt": apt, "pypi": pypi}[args.kind](args)


if __name__ == "__main__":
    main()

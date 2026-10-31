"""Collects new release files for packages.yml.

  pkg_sync.py <kind> <out dir>

Reads packages/sources.toml, asks the live index which files it already has (state.json), and
downloads every matching release asset it lacks into <out dir>, checking each against the SHA-256
GitHub recorded for it. Prints "count=<n>" last. PKG_BASE_URL is the live index (<site>/pypi);
GITHUB_TOKEN raises the API rate limit.
"""
import hashlib
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def request(url, accept="application/vnd.github+json"):
    headers = {"User-Agent": "ci-publish-pkg-sync", "Accept": accept}
    if os.environ.get("GITHUB_TOKEN") and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def published(base_url):
    try:
        with request(f"{base_url.rstrip('/')}/state.json") as r:
            state = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return set()  # first publish
        raise
    return {name for files in state.values() for name in files}


def main():
    kind, out = sys.argv[1], Path(sys.argv[2])
    cfg = tomllib.loads((ROOT / "packages" / "sources.toml").read_text())[kind]
    have = published(os.environ.get("PKG_BASE_URL") or sys.exit("set PKG_BASE_URL"))
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in cfg.get("repo", []):
        with request(f"https://api.github.com/repos/{src['repo']}/releases?per_page=100") as r:
            releases = json.loads(r.read())
        for rel in releases:
            if rel["draft"] or rel["prerelease"]:
                continue
            for a in rel["assets"]:
                if a["name"] in have or not any(re.fullmatch(p, a["name"]) for p in src["assets"]):
                    continue
                digest = a.get("digest") or ""
                if not digest.startswith("sha256:"):
                    sys.exit(f"{src['repo']} {rel['tag_name']}: GitHub has no SHA-256 for {a['name']}")
                h = hashlib.sha256()
                dest = out / a["name"]
                with request(a["url"], accept="application/octet-stream") as r, open(dest, "wb") as f:
                    for chunk in iter(lambda: r.read(1 << 20), b""):
                        h.update(chunk)
                        f.write(chunk)
                if h.hexdigest() != digest.split(":", 1)[1]:
                    sys.exit(f"{a['name']}: SHA-256 mismatch with GitHub's record")
                print(f"new {src['repo']} {rel['tag_name']} {a['name']}")
                have.add(a["name"])
                count += 1
    print(f"count={count}")


if __name__ == "__main__":
    main()

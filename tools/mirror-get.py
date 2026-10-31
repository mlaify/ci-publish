#!/usr/bin/env python3
"""Fetches a file from the lab mirror and checks it against the SHA-256 pinned in the manifest.

  mirror-get.py <manifest path> [output file]

The manifest is read from this checkout (MIRROR_MANIFEST overrides), never from the mirror,
so a tampered file on the server is caught. MIRROR_URL is the mirror's base URL. Credentials
come from ~/.netrc (machine mirror.<domain> login ... password ...) when you're outside the
networks the mirror allows.
"""
import hashlib
import os
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    path = sys.argv[1]
    out = Path(sys.argv[2] if len(sys.argv) == 3 else path.rsplit("/", 1)[-1])
    manifest = Path(os.environ.get("MIRROR_MANIFEST", HERE.parent / "mirror" / "manifest.toml"))
    base = os.environ.get("MIRROR_URL") or sys.exit("set MIRROR_URL, e.g. https://mirror.example.org")

    entries = {e["path"]: e for e in tomllib.loads(manifest.read_text()).get("file", [])}
    if path not in entries:
        sys.exit(f"{path} is not in {manifest}")
    want = entries[path]["sha256"].lower()

    url = f"{base.rstrip('/')}/{path}"
    host = urllib.parse.urlsplit(url).hostname
    opener = urllib.request.build_opener()
    try:
        import netrc
        creds = netrc.netrc().authenticators(host)
    except (FileNotFoundError, netrc.NetrcParseError):
        creds = None
    if creds:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, creds[0], creds[2])
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))

    tmp = out.with_name(out.name + ".part")
    h = hashlib.sha256()
    with opener.open(url, timeout=120) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != want:
        tmp.unlink()
        sys.exit(f"SHA-256 mismatch for {path}: got {h.hexdigest()}, manifest says {want}")
    tmp.replace(out)
    print(f"{out}: OK ({want})")


if __name__ == "__main__":
    main()

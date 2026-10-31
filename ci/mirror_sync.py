"""Downloads each file in mirror/manifest.toml, checks its pinned SHA-256 and publishes it to
the mirror channel together with a <name>.sha256 file.

A file whose <name>.sha256 on the mirror already matches is skipped; that check needs
MIRROR_URL (https://mirror.<domain>) and MIRROR_AUTH (user:password for the mirror's basic
auth). Without them every file is downloaded and published again.
"""
import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

PUBLISH = Path(__file__).resolve().parent.parent / ".github" / "actions" / "publish" / "publish.sh"
PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$")


def already_published(path, sha):
    base, auth = os.environ.get("MIRROR_URL"), os.environ.get("MIRROR_AUTH")
    if not base or not auth:
        return False
    req = urllib.request.Request(f"{base.rstrip('/')}/{path}.sha256")
    req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode().split()[0] == sha
    except (urllib.error.URLError, IndexError, UnicodeDecodeError):
        return False


def download(url, dest, want):
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "ci-publish-mirror-sync"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        size = int(r.headers.get("Content-Length") or 0)
        free = shutil.disk_usage(dest.parent).free
        if size and size > free - (1 << 30):
            raise RuntimeError(f"{size} bytes won't fit in {free} free; add this file by hand")
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != want:
        raise RuntimeError(f"SHA-256 mismatch: got {h.hexdigest()}, manifest says {want}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--only", nargs="*", default=[], help="sync only these paths")
    ap.add_argument("--since", help="sync only entries added or changed since this git commit")
    args = ap.parse_args()

    entries = tomllib.loads(Path(args.manifest).read_text()).get("file", [])
    if args.since:
        old = subprocess.run(["git", "show", f"{args.since}:{args.manifest}"], capture_output=True, text=True)
        before = {(e["path"], e["sha256"]) for e in tomllib.loads(old.stdout).get("file", [])} if old.returncode == 0 else set()
        entries = [e for e in entries if (e["path"], e["sha256"].lower()) not in before]
        print(f"{len(entries)} new or changed since {args.since[:12]}")
    failed = 0
    for e in entries:
        path, url, sha = e["path"], e["url"], e["sha256"].lower()
        if args.only and path not in args.only:
            continue
        if not PATH_RE.match(path) or ".." in path or not re.fullmatch(r"[0-9a-f]{64}", sha):
            print(f"SKIP {path}: bad path or sha256 in manifest", file=sys.stderr)
            failed += 1
            continue
        if already_published(path, sha):
            print(f"ok   {path} (already on the mirror)")
            continue
        with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as tmp:
            payload = Path(tmp) / "payload"
            payload.mkdir()
            name = path.rsplit("/", 1)[1]
            try:
                download(url, payload / name, sha)
            except Exception as exc:  # noqa: BLE001 - report and carry on with the next file
                print(f"FAIL {path}: {exc}", file=sys.stderr)
                failed += 1
                continue
            (payload / f"{name}.sha256").write_text(f"{sha}  {name}\n")
            env = dict(os.environ, CHANNEL="mirror", SOURCE=str(payload), DEST=path.rsplit("/", 1)[0])
            subprocess.run([str(PUBLISH)], env=env, check=True)
            print(f"sent {path}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

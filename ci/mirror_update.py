"""Finds the latest stable release of each source in mirror/sources.toml, verifies it, and rewrites
the managed block of mirror/manifest.toml. Prints "changed" or "unchanged".

Distribution ISOs are verified against their signed checksum files (gpg, pinned keys); GitHub
release assets against the SHA-256 GitHub records, after a cool-off. Anything that fails
verification stops the run: a source is never mirrored on an unverified hash.
"""
import datetime
import email.utils
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BEGIN = "# BEGIN managed by ci/mirror_update.py: edit mirror/sources.toml, not this block"
END = "# END managed by ci/mirror_update.py"
UA = {"User-Agent": "ci-publish-mirror-update"}


def get(url, headers=None, attempts=3):
    """GET with retries for dropped connections and 5xx; a 4xx is an answer, not a hiccup."""
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == attempts:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


class Gpg:
    """A throwaway keyring. verify() returns the primary fingerprint of a good signature."""

    def __init__(self, home):
        self.home = home

    def run(self, *args, data=None):
        return subprocess.run(["gpg", "--homedir", self.home, "--batch", "--status-fd", "1", *args],
                              input=data, capture_output=True, check=False)

    def import_keys(self, blob):
        self.run("--import", data=blob)

    def uid_of(self, fpr):
        out = self.run("--with-colons", "--list-keys", fpr).stdout.decode()
        return [line.split(":")[9] for line in out.splitlines() if line.startswith("uid:")]

    def verify(self, *files):
        out = self.run("--verify", *files).stdout.decode()
        for line in out.splitlines():
            if line.startswith("[GNUPG:] VALIDSIG "):
                return line.split()[-1]
        raise RuntimeError(f"no valid signature on {files[-1]}")


def ubuntu(cfg, gpg, tmp):
    gpg.import_keys(get(f"https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x{cfg['key']}"))
    entries = []
    for series in cfg["series"]:
        base = f"https://releases.ubuntu.com/{series}"
        sums, sig = tmp / "u.sums", tmp / "u.sig"
        sums.write_bytes(get(f"{base}/SHA256SUMS"))
        sig.write_bytes(get(f"{base}/SHA256SUMS.gpg"))
        if gpg.verify(str(sig), str(sums)) != cfg["key"]:
            raise RuntimeError(f"Ubuntu {series}: SHA256SUMS not signed by the pinned key")
        for image in cfg["images"]:
            found = []
            for line in sums.read_text().splitlines():
                h, name = line.split(maxsplit=1)
                name = name.lstrip("*")
                m = re.fullmatch(rf"ubuntu-({re.escape(series)}(?:\.\d+)?)-{re.escape(image)}", name)
                if m:
                    found.append((tuple(int(x) for x in m.group(1).split(".")), m.group(1), name, h))
            if not found:
                raise RuntimeError(f"Ubuntu {series}: no {image}")
            _, ver, name, h = max(found)
            entries.append({"path": f"ubuntu/{ver}/{name}", "url": f"https://releases.ubuntu.com/{ver}/{name}",
                            "sha256": h})
    return entries


def fedora(cfg, gpg, tmp):
    gpg.import_keys(get(cfg["keyring"]))
    releases = json.loads(get("https://fedoraproject.org/releases.json"))
    stable = [int(r["version"]) for r in releases if str(r.get("version", "")).isdigit()]
    version = max(stable)
    entries = []
    for variant in cfg["variants"]:
        base = f"https://dl.fedoraproject.org/pub/fedora/linux/releases/{version}/{variant}/x86_64/iso"
        listing = get(base + "/").decode()
        names = re.findall(r'href="([^"]+-CHECKSUM)"', listing)
        if len(names) != 1:
            raise RuntimeError(f"Fedora {version} {variant}: expected one CHECKSUM file, found {names}")
        chk = tmp / "f.chk"
        chk.write_bytes(get(f"{base}/{names[0]}"))
        fpr = gpg.verify(str(chk))
        if not any(uid.startswith(f"Fedora ({version})") for uid in gpg.uid_of(fpr)):
            raise RuntimeError(f"Fedora {version} {variant}: CHECKSUM signed by {fpr}, not the Fedora {version} key")
        isos = re.findall(r"^SHA256 \(([^)]+\.iso)\) = ([0-9a-f]{64})$", chk.read_text(), re.M)
        if len(isos) != 1:
            raise RuntimeError(f"Fedora {version} {variant}: expected one ISO, found {len(isos)}")
        name, h = isos[0]
        entries.append({"path": f"fedora/{version}/{variant.lower()}/{name}", "url": f"{base}/{name}", "sha256": h})
    return entries


def kali(cfg, gpg, tmp):
    gpg.import_keys(get("https://archive.kali.org/archive-key.asc"))
    sums, sig = tmp / "k.sums", tmp / "k.sig"
    sums.write_bytes(get("https://cdimage.kali.org/current/SHA256SUMS"))
    sig.write_bytes(get("https://cdimage.kali.org/current/SHA256SUMS.gpg"))
    if gpg.verify(str(sig), str(sums)) != cfg["key"]:
        raise RuntimeError("Kali: SHA256SUMS not signed by the pinned key")
    entries = []
    for image in cfg["images"]:
        hits = re.findall(rf"^([0-9a-f]{{64}})\s+(kali-linux-(\d{{4}}\.\d+)-{re.escape(image)})$", sums.read_text(), re.M)
        if len(hits) != 1:
            raise RuntimeError(f"Kali: expected one {image}, found {len(hits)}")
        h, name, ver = hits[0]
        entries.append({"path": f"kali/{ver}/{name}", "url": f"https://cdimage.kali.org/kali-{ver}/{name}", "sha256": h})
    return entries


def ago(cfg_days):
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=cfg_days)


def parse_time(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def gh_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return headers


def safe(name):
    """A path component mirror_sync.py accepts (same rule it applies: letters, digits, . _ -)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def asset_patterns(src):
    return [src["asset"]] if isinstance(src["asset"], str) else src["asset"]


def github(cfg):
    cutoff = ago(cfg["min_age_days"])
    entries = []
    for src in cfg["release"]:
        releases = json.loads(get(f"https://api.github.com/repos/{src['repo']}/releases?per_page=30", gh_headers()))
        patterns = asset_patterns(src)
        pick = None
        for rel in releases:  # newest first
            if rel["draft"] or rel["prerelease"] or parse_time(rel["published_at"]) > cutoff:
                continue
            found = [[a for a in rel["assets"] if re.fullmatch(p, a["name"])] for p in patterns]
            if all(found):
                pick = (rel, found)
                break
        if not pick:
            raise RuntimeError(f"{src['name']}: no stable release older than {cfg['min_age_days']} days has all of {patterns}")
        rel, found = pick
        tag = safe(rel["tag_name"])
        for pattern, assets in zip(patterns, found):
            if len(assets) != 1:
                raise RuntimeError(f"{src['name']}: {len(assets)} assets match {pattern} in {rel['tag_name']}")
            a = assets[0]
            digest = a.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise RuntimeError(f"{src['name']}: GitHub has no SHA-256 for {a['name']}")
            entries.append({"path": f"tools/{src['name']}/{tag}/{safe(a['name'])}", "url": a["browser_download_url"],
                            "sha256": digest.split(":", 1)[1]})
    return entries


def signed_sums(gpg, tmp, sums_url, sig_url, key):
    """Downloads a checksum list and its detached signature; returns the text if the pinned key signed it."""
    sums, sig = tmp / "sums", tmp / "sums.sig"
    sums.write_bytes(get(sums_url))
    sig.write_bytes(get(sig_url))
    if gpg.verify(str(sig), str(sums)) != key:
        raise RuntimeError(f"{sums_url}: not signed by the pinned key {key}")
    return sums.read_text()


def keyserver(gpg, key):
    gpg.import_keys(get(f"https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x{key}"))


def debian(cfg, gpg, tmp):
    keyserver(gpg, cfg["key"])
    base = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd"
    text = signed_sums(gpg, tmp, f"{base}/SHA256SUMS", f"{base}/SHA256SUMS.sign", cfg["key"])
    hits = re.findall(r"^([0-9a-f]{64})\s+(debian-(\d+\.\d+\.\d+)-amd64-netinst\.iso)$", text, re.M)
    if len(hits) != 1:
        raise RuntimeError(f"Debian: expected one netinst ISO, found {len(hits)}")
    h, name, ver = hits[0]
    return [{"path": f"debian/{ver}/{name}", "url": f"{base}/{name}", "sha256": h}]


def alpine(cfg, gpg, tmp):
    keyserver(gpg, cfg["key"])
    entries = []
    for arch in cfg["arches"]:
        base = f"https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/{arch}"
        meta = get(f"{base}/latest-releases.yaml").decode()
        iso = re.search(rf"iso: (alpine-virt-([0-9.]+)-{re.escape(arch)}\.iso)", meta)
        if not iso:
            raise RuntimeError(f"Alpine {arch}: no virt ISO in latest-releases.yaml")
        name, ver = iso.group(1), iso.group(2)
        h = get(f"{base}/{name}.sha256").decode().split()[0]
        # Alpine signs the ISO itself, not a checksum list, so the (small) ISO is fetched and checked here.
        iso_path = tmp / name
        iso_path.write_bytes(get(f"{base}/{name}"))
        (tmp / "a.asc").write_bytes(get(f"{base}/{name}.asc"))
        if gpg.verify(str(tmp / "a.asc"), str(iso_path)) != cfg["key"]:
            raise RuntimeError(f"Alpine {name}: not signed by the pinned key")
        if sha256_file(iso_path) != h:
            raise RuntimeError(f"Alpine {name}: .sha256 doesn't match the signed ISO")
        iso_path.unlink()
        major = ".".join(ver.split(".")[:2])
        entries.append({"path": f"alpine/{ver}/{name}", "sha256": h,
                        "url": f"https://dl-cdn.alpinelinux.org/alpine/v{major}/releases/{arch}/{name}"})
    return entries


def ubuntu_cloud(cfg, gpg, tmp):
    keyserver(gpg, cfg["key"])
    entries = []
    for series in cfg["series"]:
        listing = get(f"https://cloud-images.ubuntu.com/releases/{series}/").decode()
        serial = max(re.findall(r"release-(\d{8})/", listing))
        base = f"https://cloud-images.ubuntu.com/releases/{series}/release-{serial}"
        text = signed_sums(gpg, tmp, f"{base}/SHA256SUMS", f"{base}/SHA256SUMS.gpg", cfg["key"])
        for arch in cfg["arches"]:
            name = f"ubuntu-{series}-server-cloudimg-{arch}.img"
            hits = re.findall(rf"^([0-9a-f]{{64}}) \*?{re.escape(name)}$", text, re.M)
            if len(hits) != 1:
                raise RuntimeError(f"Ubuntu cloud {series} {serial}: no {name}")
            entries.append({"path": f"ubuntu-cloud/{series}/{serial}/{name}", "url": f"{base}/{name}", "sha256": hits[0]})
    return entries


def wireshark(cfg, gpg, tmp):
    keyserver(gpg, cfg["key"])
    listing = get("https://www.wireshark.org/download/").decode()
    # Stable releases have an even minor version (4.6.x); odd minors are development builds.
    versions = {tuple(int(x) for x in v.split(".")) for v in re.findall(r"SIGNATURES-(\d+\.\d+\.\d+)\.txt", listing)}
    stable = max(v for v in versions if v[1] % 2 == 0)
    ver = ".".join(map(str, stable))
    sig = tmp / "ws.txt"
    sig.write_bytes(get(f"https://www.wireshark.org/download/SIGNATURES-{ver}.txt"))
    if gpg.verify(str(sig)) != cfg["key"]:
        raise RuntimeError(f"Wireshark {ver}: SIGNATURES not signed by the pinned key")
    text = sig.read_text()
    entries = []
    for f in cfg["files"]:
        name = f["name"].format(version=ver)
        m = re.search(rf"^SHA256\({re.escape(name)}\)=([0-9a-f]{{64}})$", text, re.M)
        if not m:
            raise RuntimeError(f"Wireshark {ver}: no SHA256 for {name}")
        entries.append({"path": f"tools/wireshark/{ver}/{safe(name)}", "sha256": m.group(1),
                        "url": f"https://2.na.dl.wireshark.org/{f['dir']}/" + urllib.parse.quote(name)})
    return entries


def remnux(cfg):
    """REMnux prints each file's SHA-256 next to its link on the docs page (HTTPS, not signed)."""
    page = get("https://docs.remnux.org/install-distro/get-virtual-appliance").decode()
    events = [(m.start(), "url", m.group(0)) for m in
              re.finditer(r"https://download\.remnux\.org/[0-9a-z]+/remnux-[a-z0-9-]+\.(?:ova|zip)", page)]
    events += [(m.start(), "hash", m.group(0)) for m in re.finditer(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", page)]
    pairs, last = {}, None
    for _, kind, value in sorted(events):
        if kind == "url":
            last = value
        elif last and last not in pairs and value not in pairs.values():
            pairs[last] = value
    entries = []
    for want in cfg["files"]:
        urls = [u for u in pairs if u.endswith("/" + want)]
        if len(urls) != 1:
            raise RuntimeError(f"REMnux: {want} not found with a hash on the docs page")
        url = urls[0]
        build = url.split("/")[-2]
        modified = urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA), timeout=60).headers["Last-Modified"]
        if email.utils.parsedate_to_datetime(modified) > ago(cfg["min_age_days"]):
            raise RuntimeError(f"REMnux {build}: newer than {cfg['min_age_days']} days; keep the previous build")
        entries.append({"path": f"remnux/{build}/{want}", "url": url, "sha256": pairs[url]})
    return entries


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_url(url):
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def first_seen(cfg, known):
    """Sources that publish no checksum. The hash is taken the first time a version is seen and kept
    from then on (a changed file then fails the sync); the weekly pull request is where it is reviewed."""
    cutoff = ago(cfg["min_age_days"])
    entries = []

    def add(path, url):
        entries.append({"path": path, "url": url, "sha256": known.get(url) or sha256_url(url)})

    for src in cfg.get("github_tag", []):
        releases = json.loads(get(f"https://api.github.com/repos/{src['repo']}/releases?per_page=30", gh_headers()))
        rel = next((r for r in releases if not (r["draft"] or r["prerelease"]) and parse_time(r["published_at"]) <= cutoff), None)
        if not rel:
            raise RuntimeError(f"{src['name']}: no stable release older than {cfg['min_age_days']} days")
        tag = rel["tag_name"]
        add(f"{src['dir']}/{src['name']}/{safe(tag)}/{src['name']}-{safe(tag)}.tar.gz",
            f"https://github.com/{src['repo']}/archive/refs/tags/{urllib.parse.quote(tag)}.tar.gz")
    for src in cfg.get("github_branch", []):
        until = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        commit = json.loads(get(f"https://api.github.com/repos/{src['repo']}/commits?sha={src['branch']}&until={until}&per_page=1",
                                gh_headers()))[0]
        sha, day = commit["sha"], commit["commit"]["committer"]["date"][:10]
        add(f"{src['dir']}/{src['name']}/{day}-{sha[:12]}/{src['name']}-{sha[:12]}.zip",
            f"https://github.com/{src['repo']}/archive/{sha}.zip")
    for src in cfg.get("vagrant", []):
        box = json.loads(get(f"https://vagrantcloud.com/api/v2/box/{src['box']}"))["current_version"]
        provider = next(p for p in box["providers"] if p["name"] == src["provider"])
        add(f"{src['dir']}/{src['name']}/{safe(box['version'])}/{src['name']}-{src['provider']}.box", provider["download_url"])
    return entries


def check_downloadable(entries):
    """Fetches the first byte of each file: a checksum list can name files that aren't served."""
    for e in entries:
        req = urllib.request.Request(e["url"], headers={**UA, "Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read(1)
        except Exception as exc:  # noqa: BLE001 - name the file, then stop
            raise RuntimeError(f"{e['path']}: not downloadable from {e['url']} ({exc})") from exc


def render(entries):
    lines = [BEGIN]
    for e in sorted(entries, key=lambda e: e["path"]):
        lines += ["[[file]]", f"path = {json.dumps(e['path'])}", f"url = {json.dumps(e['url'])}",
                  f"sha256 = {json.dumps(e['sha256'])}", ""]
    while lines[-1] == "":
        lines.pop()
    lines.append(END)
    return "\n".join(lines) + "\n"


def main():
    sources = tomllib.loads((ROOT / "mirror" / "sources.toml").read_text())
    manifest = ROOT / "mirror" / "manifest.toml"
    known = {e["url"]: e["sha256"] for e in tomllib.loads(manifest.read_text()).get("file", [])}
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (tmp / "gnupg").mkdir(mode=0o700)
        gpg = Gpg(str(tmp / "gnupg"))
        entries = []
        entries += ubuntu(sources["ubuntu"], gpg, tmp)
        entries += fedora(sources["fedora"], gpg, tmp)
        entries += kali(sources["kali"], gpg, tmp)
        entries += debian(sources["debian"], gpg, tmp)
        entries += alpine(sources["alpine"], gpg, tmp)
        entries += ubuntu_cloud(sources["ubuntu_cloud"], gpg, tmp)
        entries += wireshark(sources["wireshark"], gpg, tmp)
        entries += github(sources["github"])
        entries += remnux(sources["remnux"])
        entries += first_seen(sources["first_seen"], known)
    check_downloadable(entries)

    text = manifest.read_text()
    block = render(entries)
    if BEGIN in text:
        start, end = text.index(BEGIN), text.index(END) + len(END) + 1
        new = text[:start] + block + text[end:]
    else:
        new = text.rstrip("\n") + "\n\n" + block
    tomllib.loads(new)  # never write a manifest that doesn't parse
    if new == text:
        print("unchanged")
        return
    manifest.write_text(new)
    for e in sorted(entries, key=lambda e: e["path"]):
        print(f"  {e['path']}")
    print("changed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - one clear line for the workflow log
        print(f"mirror_update: {exc}", file=sys.stderr)
        sys.exit(1)

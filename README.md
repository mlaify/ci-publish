# ci-publish

Publishing from GitHub Actions to a plain static web host (SSH, cron, `.htaccess`; no root, no
containers, no long-running processes): security reports, a lab mirror, package repositories and
documentation sites. The host keeps no secrets, and nothing runs on it but a five-minute cron job.

| Channel | Site | Holds | Who can read it | Fed by |
|---|---|---|---|---|
| `reports` | `reports.<domain>` | Security scans per repo and commit: Trivy (vulnerabilities, secrets, misconfigurations), a CycloneDX SBOM, Semgrep, SARIF; and leak scans under `leaks/` | Your network, or basic auth | `security-scan`, `leak-scan` + `publish` actions |
| `mirror` | `mirror.<domain>` | Lab VM images, ISOs, toolchains, wordlists, each pinned by SHA-256 | Your network, or basic auth | `mirror.yml` from `mirror/manifest.toml` |
| `pkg` | `pkg.<domain>` | A signed APT repo, a pip (PEP 503) index, a Helm chart repo | Public | `pkg-index` + `publish` actions |
| `docs` | One web root per site, each with as many domains as you like | Static sites (MkDocs, Hugo) | Public | `publish` action |
| `landing` | The root page of mirror and pkg | One `index.html` each | As the site | `landing.yml` |

Host names, accounts, addresses and passwords live in GitHub secrets and variables, never in this
repository.

## How publishing works

```
GitHub Actions ──rsync over SSH──> ~/ci-drop/<channel>/<id>/      (write-only key; can't read or delete)
                                  ~/ci-drop/<channel>/<id>.ready  (written last: "upload complete")
cron, every 5 min ──────────────> host/promote.sh
                                  checks the upload, then moves it into the channel's web root
```

The CI key's forced command is `rrsync -wo ~/ci-drop`. A stolen key can drop files, but it can't
get a shell, read anything, or write to a web root directly. `promote.sh` then refuses any upload
containing:
- anything the web server could execute: `.php`, `.phtml`, `.phar`, `.cgi`, `.pl`, `.py`, `.sh`
  and the like, in any part of the name (`x.php.png` counts);
- anything under a `cgi-bin/` folder;
- dotfiles such as `.htaccess` or `.user.ini` (except `.well-known/`);
- symlinks, or unusual characters in a name;
- a destination the channel doesn't allow.

Refused uploads go to `~/promote/rejected/` for 14 days, and every decision is logged in
`~/promote/log`. Files are moved, not copied, so a half-written ISO is never served. Package indexes
are moved after their packages, so an index never points at a file that isn't there yet.

**Root pages.** `landing.yml` builds the front pages of mirror and pkg (`ci/landing.py`) and sends
each through the `landing` channel, which accepts one `index.html` and puts it at that site's root.
The reports front page lists every repository with its latest summary; `promote.sh` rewrites it
after each report, and links the leak scan's own front page once there is one.

## Docs sites: several sites, several domains each

A docs site is a name in `DOCS_SITES` (`~/promote/config.env`) mapped to a web root. CI publishes by
name; a name that isn't listed is refused. Each publish replaces that web root whole, so:
- a root must be a domain's `public_html`, or a folder in it;
- no root may contain another site's root;
- the host owns `.htaccess`, `.user.ini`, `cgi-bin/` and the ACME challenge folder, and a publish
  never touches them.

No path ever comes from CI, and a bad config line stops publishing instead of widening what a
publish deletes: a deploy that runs `rsync --delete` through an empty path variable can wipe a whole
account.

A site answers on every name its domain has; add extra names as aliases of the domain (not
redirects). See `host/config.env.example`.

## Host setup (once)

1. **Domains.** Add `reports.<domain>`, `mirror.<domain>`, `pkg.<domain>` and each docs site.
   - Serve `reports` and `mirror` directly, not through a proxy or CDN: their `Require ip` rule needs
     the visitor's real address, and the mirror serves multi-GB files.
   - `pkg` and the docs sites can sit behind a CDN. If the CDN injects scripts into HTML (Cloudflare's
     bot detection does), `pkg.htaccess` already sends `Cache-Control: no-transform`.
2. **Check the host tools.** You need bash 4+, GNU `find`/`date`, `rsync`, and `rrsync` at
   `/usr/bin/rrsync` (3.2.4+ for `-no-lock`):
   ```
   bash --version | head -1; rsync --version | head -1; ls -l /usr/bin/rrsync
   ```
3. **Directories and the promote script:**
   ```
   mkdir -p ~/ci-drop/{reports,mirror,pkg,docs,landing} ~/promote
   # copy host/promote.sh to ~/promote/promote.sh, then:
   chmod 700 ~/promote/promote.sh
   # copy host/config.env.example to ~/promote/config.env and set the roots and DOCS_SITES
   ```
4. **.htaccess files.**
   - Copy `host/htaccess/<channel>.htaccess` to each site's `public_html/.htaccess`. Docs sites
     use `docs.htaccess`.
   - Replace `USER` with the account name and `HOME_CIDR` with the network allowed without a password.
   - Create the password files with bcrypt (`htpasswd -B -c ~/.htpasswd-reports <name>`, and the
     same for `-mirror`), or through your hosting panel. Add a separate `ci` login to each for the
     workflows that read the sites (`REPORTS_AUTH`, `MIRROR_AUTH`).
5. **CI keys.** One per publishing repository, so you can revoke one alone: see
   `tools/add-publisher.sh` below.
6. **Cron.** `*/5 * * * * /home/USER/promote/promote.sh`.
7. **Test.** `tests/test_promote.sh` runs here first. After the first real publish, check
   `~/promote/log` on the host.

## GitHub setup

This repository is public, so any repository can use its actions:
`uses: <owner>/ci-publish/.github/actions/<action>@<SHA>`. Pin every reference by commit SHA, never
`@main`, so a change here doesn't reach a repository unreviewed.

**`tools/add-publisher.sh <owner>/<repo>`** sets up one publishing repository. It checks the host
key against the fingerprint you give it, creates the `ci-publish` environment limited to `main`,
generates the publishing key and stores it and the host details as environment secrets without
printing them, then prints the one `authorized_keys` line to add on the host. Running it again
replaces the key, so delete the old line afterwards.

Secrets and variables (environment `ci-publish`, deployable from `main` only):

| Name | Kind | Value |
|---|---|---|
| `PUBLISH_SSH_KEY` | secret | That repository's private CI key |
| `PUBLISH_KNOWN_HOSTS` | secret | The host's key line, its fingerprint checked out of band |
| `PUBLISH_HOST`, `PUBLISH_USER` | secret | The SSH host name and the account name |
| `REPORTS_URL`, `REPORTS_AUTH` | secret | This repository: the reports site and `ci:<password>`, so unchanged repositories are skipped |
| `MIRROR_URL`, `MIRROR_AUTH` | secret | This repository: the mirror and `ci:<password>`, so unchanged files are skipped |
| `APT_SIGNING_KEY` | secret | APT publishing only: an armored key used for nothing but signing this repository |
| `PKG_URL` | variable | The package site, e.g. `https://pkg.example.org` |

Workflows that hold secrets run only on `main` (the environment's branch rule), so pull requests
from forks never see them. Keep "Require approval for first-time contributors" on.

Example workflows to copy:
- `examples/security-reports.yml` (reports)
- `examples/publish-packages.yml` (pkg)
- `examples/publish-docs.yml` (a Hugo site)

## Using it

**Reports.** `reports.<domain>/<repo>/` lists every commit with a one-line summary, and `latest/`
is the newest.

**Scanning public repositories** (`scan-public.yml`, nightly and on demand). Every public,
non-archived repository of the organization whose default branch has moved since its last report
is scanned and published. The scan legs get no secrets; only the publish legs hold the key.

**Private repositories and leak scans** are meant for a private companion repository that uses
these actions with its own credentials, so neither private code nor findings appear in this
repository's public logs:
- `security-scan` over each private repository;
- `leak-scan` over every repository, with gitleaks' secret rules plus a private rule set passed in
  `LEAK_RULES` (a gitleaks TOML config, kept in a secret: it describes what must not leak). It scans
  the current files and the whole history, redacts every value, and leaves out commit authors and
  messages. Reports go to `reports.<domain>/leaks/<repo>/`; findings in a public repository are
  marked as exposed.

**Mirror.**
- **Automatic sources.** `mirror/sources.toml` lists what the mirror follows, always the latest
  stable release:
  - Distributions: Ubuntu Server, Fedora Silverblue and Workstation, Kali (installer and QEMU
    image), Debian netinst, Alpine virt (x86_64, aarch64), and Ubuntu cloud images (amd64, arm64).
  - Lab VMs: REMnux (amd64 OVA, arm64 UTM) and Metasploitable3 (intentionally vulnerable; isolated
    networks only).
  - Tools: Ghidra with Temurin JDK 21, CyberChef, Wireshark, radare2, Cutter, Volatility 3, YARA,
    Firecracker, Lima and FLARE-VM.
  - Wordlists: SecLists.
  - Skipped on purpose, because their licences forbid redistribution: Windows evaluation ISOs,
    Sysinternals and Burp Suite.
- **How each source is trusted.**
  - Most are verified against the publisher's signature, with the key pinned in `sources.toml`.
  - GitHub assets and REMnux are checked against a hash only (GitHub's, or REMnux's HTTPS docs
    page), so they wait 14 days first.
  - SecLists, FLARE-VM, the YARA source and Metasploitable3 publish no hash at all. Their hash is
    taken the first time a version appears and kept after that, so a file that later changes
    upstream fails the sync. Review those in the weekly PR.
  - Every Tuesday, `mirror-update.yml` resolves the latest releases and verifies them as above.
  - It opens a pull request when something is new, and merging it runs the sync. This needs
    Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests".
- **One-off files.** Add them by hand above the managed block in `mirror/manifest.toml`, with the
  hash from the upstream's *signed* checksum file.
- **What a sync sends.** A merge syncs only the entries it added or changed. A manual run
  (Actions → mirror → Run workflow) syncs everything, or the paths you list, skipping files already
  on the mirror when `MIRROR_URL` and `MIRROR_AUTH` are set.

From a lab VM:
```
MIRROR_URL=https://mirror.example.org tools/mirror-get.py ubuntu/26.04.1/ubuntu-26.04.1-live-server-amd64.iso
```
The file is checked against the manifest in your checkout, not against anything on the server.
Old versions stay on the mirror until you delete them.

**APT.**
- Get the key: `curl -fsSL <PKG_URL>/apt/key.asc | sudo gpg --dearmor -o /etc/apt/keyrings/<name>.gpg`.
  Compare its fingerprint with the one you generated before trusting it.
- Add the source: `deb [signed-by=/etc/apt/keyrings/<name>.gpg] <PKG_URL>/apt stable main`.

**Packages (pip).** `packages.yml` is the only publisher of the pip index:
- Every 6 hours it collects the wheel and sdist files attached to the GitHub releases of the
  repositories in `packages/sources.toml`, checks each against GitHub's SHA-256, and adds the new
  ones to `<PKG_URL>/pypi`. To add a project, add its repository there.
- Releases come from the projects themselves (a `release.yml` that builds on a `v*` tag and attaches
  the files to a GitHub release). Nothing here builds them.

Installing these packages needs PyPI too, for their dependencies. **Dependency confusion is the
risk:** with `pip --extra-index-url`, pip takes the highest version from either index, so anyone who
registers one of these names on PyPI with a bigger version number wins. Two defences:
1. **Pin the packages to this index with uv** (preferred):
   ```toml
   # pyproject.toml of whatever installs them
   [[tool.uv.index]]
   name = "mlaify"
   url = "<PKG_URL>/pypi/simple/"
   explicit = true            # only for the packages pinned below

   [tool.uv.sources]
   attackmap = { index = "mlaify" }
   ```
   One-off: `uv tool install attackmap --index mlaify=<PKG_URL>/pypi/simple/ --index-strategy first-index`.
2. **Hold the names on PyPI** so nobody else can register them.

**Helm.** `helm repo add <name> <PKG_URL>/helm`.

## Rules

- **No secrets, host details or personal details in this repository.** It is public, and so are its
  workflow logs and artifacts.
- **No secrets on the host.** Trivy masks the values it finds, but a secret finding still means
  rotate that secret.
- **No malware samples, exploits or PoCs** in the mirror or anywhere else.
- **Reports stay private.** They map out weak spots.

## Known limits

- **Package publishes can race.** The live index is the state, so two publishes of the same kind at
  once can drop an entry. Publish each kind from one repository; its concurrency group then
  serializes the runs. Republishing the package restores a lost entry.
- **Big mirror files.** A GitHub-hosted runner has about 14 GB of disk, so files over about 12 GB
  go up by hand, with their `.sha256` alongside.
- **Promotion delay.** Uploads appear within about 5 minutes (the cron interval), not immediately.
- **Nothing is deleted automatically** except what a docs publish replaces. Old reports and mirror
  files stay until you remove them.

## Files

```
host/promote.sh                         runs on the host from cron
host/config.env.example                 its settings, including DOCS_SITES
host/authorized_keys.example            your login and a CI key line
host/htaccess/*.htaccess                one per channel
.github/actions/publish/                upload to the drop (all channels)
.github/actions/security-scan/          Trivy + Semgrep + HTML report
.github/actions/leak-scan/              gitleaks over files and history + HTML report
.github/actions/pkg-index/              APT / pip / Helm index merge
.github/workflows/scan-public.yml       nightly: scan public repositories that changed
.github/workflows/security-reports.yml  this repository scans itself
.github/workflows/mirror.yml            mirror sync (on manifest changes)
.github/workflows/mirror-update.yml     weekly: latest verified releases -> pull request
.github/workflows/packages.yml          every 6 hours: release files -> the pip index
.github/workflows/landing.yml           root pages of mirror and pkg
.github/workflows/ci.yml                tests and linters for this repository
ci/mirror_sync.py                       download, check hash, publish
ci/mirror_update.py                     resolve and verify the latest releases
ci/pkg_sync.py                          collect new release files for packages.yml
ci/landing.py                           build the mirror and pkg root pages
packages/sources.toml                   which repositories packages.yml publishes
mirror/sources.toml                     what the updater follows, with pinned keys
mirror/manifest.toml                    what the mirror holds
tools/mirror-get.py                     fetch and check from a lab VM
tools/add-publisher.sh                  set up a publishing repository
examples/*.yml                          workflows to copy into other repositories
tests/test_promote.sh                   promote.sh against a throwaway home
```

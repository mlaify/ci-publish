#!/bin/bash
# Runs host/promote.sh against a throwaway home directory and checks what it promotes and rejects.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
fails=0
pass() { echo "ok   $1"; }
fail() { echo "FAIL $1"; fails=$((fails + 1)); }
expect() { if eval "$2"; then pass "$1"; else fail "$1"; fi; }

export HOME="$t/home"
mkdir -p "$HOME/promote" "$HOME/ci-drop"/{reports,mirror,pkg,docs}
cat >"$HOME/promote/config.env" <<'EOF'
DROP="$HOME/ci-drop"
STATE="$HOME/promote"
REPORTS_ROOT="$HOME/domains/reports.test/public_html"
MIRROR_ROOT="$HOME/domains/mirror.test/public_html"
PKG_ROOT="$HOME/domains/pkg.test/public_html"
DOCS_SITES="
site=$HOME/domains/docs.test/public_html   # a whole domain
notes=$HOME/domains/pages.test/public_html/notes
"
EOF
for d in reports mirror pkg docs pages; do mkdir -p "$HOME/domains/$d.test/public_html"; done
mkdir -p "$HOME/domains/reports.test/public_html/cgi-bin"   # some hosting panels create one in every public_html

drop() { # channel id dest file...
  local ch=$1 id=$2 dest=$3 f
  shift 3
  mkdir -p "$HOME/ci-drop/$ch/$id"
  for f in "$@"; do mkdir -p "$(dirname "$HOME/ci-drop/$ch/$id/$f")"; echo "$f" >"$HOME/ci-drop/$ch/$id/$f"; done
  echo "$dest" >"$HOME/ci-drop/$ch/$id.ready"
}

sha=0123456789abcdef0123456789abcdef01234567
drop reports r1 "app/$sha" index.html summary.txt sbom.cdx.json sarif/trivy.sarif
echo 'vulns C1 H2 <b>x</b>' >"$HOME/ci-drop/reports/r1/summary.txt"
drop reports r2 "app/$sha" index.html evil.php
drop reports r3 "app/$sha" index.html img.php.png
drop reports r4 "app/$sha" .htaccess
drop reports r5 "app/../../etc" index.html
drop reports r6 "app/notasha" index.html
drop reports r7 "app/$sha" index.html && ln -s /etc/passwd "$HOME/ci-drop/reports/r7/passwd"
drop reports k1 "leaks/app/$sha" index.html summary.txt
echo 'leaks 2 (high 1)' >"$HOME/ci-drop/reports/k1/summary.txt"
drop reports k2 "leaks/$sha" index.html
drop mirror m1 ubuntu/24.04 ubuntu.iso ubuntu.iso.sha256
drop mirror m2 tools install.sh
drop pkg p1 apt pool/main/f/foo/foo_1.0_amd64.deb dists/stable/main/binary-amd64/Packages dists/stable/Release dists/stable/InRelease key.asc
drop pkg p2 notakind index.yaml
drop docs d1 site index.html old.html .well-known/security.txt
echo keep >"$HOME/domains/docs.test/public_html/.htaccess"
mkdir -p "$HOME/domains/docs.test/public_html/.well-known/acme-challenge"
echo token >"$HOME/domains/docs.test/public_html/.well-known/acme-challenge/t"
drop docs d3 notes index.html
drop docs d4 unlisted index.html
drop docs d7 site index.html cgi-bin/run
echo placeholder >"$HOME/domains/mirror.test/public_html/index.html"
drop landing l1 mirror index.html
drop landing l2 reports index.html
drop landing l3 pkg index.html extra.html
mkdir -p "$HOME/ci-drop/mirror/stale" && touch -d '3 days ago' "$HOME/ci-drop/mirror/stale"
mkdir -p "$HOME/ci-drop/mirror/fresh"

bash "$here/../host/promote.sh"
cat "$HOME/promote/log"

expect "report promoted" "[ -f '$HOME/domains/reports.test/public_html/app/$sha/sarif/trivy.sarif' ]"
expect "latest copy made" "[ -f '$HOME/domains/reports.test/public_html/app/latest/index.html' ]"
expect "repo index lists the commit" "grep -q '${sha}/' '$HOME/domains/reports.test/public_html/app/index.html'"
expect "summary is escaped" "grep -q '&lt;b&gt;x&lt;/b&gt;' '$HOME/domains/reports.test/public_html/app/index.html'"
expect "top index lists the repo" "grep -q 'href=\"app/\"' '$HOME/domains/reports.test/public_html/index.html'"
expect "top index skips cgi-bin" "! grep -q cgi-bin '$HOME/domains/reports.test/public_html/index.html'"
expect "no php anywhere in web roots" "[ -z \"\$(find '$HOME/domains' -name '*.php*')\" ]"
expect "no symlink in web roots" "[ -z \"\$(find '$HOME/domains' -type l)\" ]"
for id in r2 r3 r4 r5 r6 r7; do
  expect "reports/$id rejected" "grep -q 'REJECT reports $id' '$HOME/promote/log' && [ -d '$HOME/promote/rejected/reports-$id' ]"
done
expect "landing page replaces the placeholder" "grep -qx index.html '$HOME/domains/mirror.test/public_html/index.html'"
expect "landing for reports refused" "grep -q 'REJECT landing l2.*mirror or pkg' '$HOME/promote/log'"
expect "landing with extra files refused" "grep -q 'REJECT landing l3.*alone' '$HOME/promote/log' && [ ! -e '$HOME/domains/pkg.test/public_html/extra.html' ]"
expect "top index shows the latest summary" "grep -q 'vulns C1 H2' '$HOME/domains/reports.test/public_html/index.html'"
expect "leak report promoted under leaks/" "[ -f '$HOME/domains/reports.test/public_html/leaks/app/latest/index.html' ]"
expect "leaks front page lists it" "grep -q 'leaks 2 (high 1)' '$HOME/domains/reports.test/public_html/leaks/index.html'"
expect "root front page links leaks/, not as a repo" "grep -q '<p><a href=\"leaks/\">' '$HOME/domains/reports.test/public_html/index.html' && ! grep -q '<td><a href=\"leaks/\">' '$HOME/domains/reports.test/public_html/index.html'"
expect "a repository can't be named leaks" "grep -q 'REJECT reports k2' '$HOME/promote/log'"
expect "mirror file promoted" "[ -f '$HOME/domains/mirror.test/public_html/ubuntu/24.04/ubuntu.iso' ]"
expect "mirror .sh rejected" "grep -q 'REJECT mirror m2' '$HOME/promote/log'"
expect "apt promoted" "[ -f '$HOME/domains/pkg.test/public_html/apt/dists/stable/InRelease' ] && [ -f '$HOME/domains/pkg.test/public_html/apt/pool/main/f/foo/foo_1.0_amd64.deb' ]"
expect "pkg bad kind rejected" "grep -q 'REJECT pkg p2' '$HOME/promote/log'"
expect "docs promoted with .well-known" "[ -f '$HOME/domains/docs.test/public_html/.well-known/security.txt' ]"
expect "docs .htaccess kept" "[ -f '$HOME/domains/docs.test/public_html/.htaccess' ]"
expect "docs site in a folder promoted" "[ -f '$HOME/domains/pages.test/public_html/notes/index.html' ]"
for id in d4 d7; do
  expect "docs/$id rejected" "grep -q 'REJECT docs $id' '$HOME/promote/log'"
done
expect "wide root deleted nothing" "[ -d '$HOME/domains/reports.test/public_html/app' ]"
expect "stale upload pruned" "[ ! -e '$HOME/ci-drop/mirror/stale' ]"
expect "fresh upload kept" "[ -d '$HOME/ci-drop/mirror/fresh' ]"
expect "drop emptied of finished uploads" "[ -z \"\$(find '$HOME/ci-drop' -name '*.ready')\" ]"

# A second docs publish replaces the site: old.html goes, the operator's .htaccess stays.
drop docs d2 site index.html
bash "$here/../host/promote.sh"
expect "docs replaced whole" "[ ! -e '$HOME/domains/docs.test/public_html/old.html' ] && [ -f '$HOME/domains/docs.test/public_html/.htaccess' ]"
expect "ACME challenge kept" "[ -f '$HOME/domains/docs.test/public_html/.well-known/acme-challenge/t' ]"

# Misconfigured roots are refused, and a root that contains other sites blocks them too:
# a bad line stops publishing, it never widens what a publish may delete.
cat >>"$HOME/promote/config.env" <<'CONF'
DOCS_SITES="$DOCS_SITES
outside=$HOME/www/x
wide=$HOME/domains
"
CONF
drop docs d5 outside index.html
drop docs d6 wide index.html
drop docs d10 site index.html
bash "$here/../host/promote.sh"
expect "root outside public_html refused" "grep -q 'REJECT docs d5.*root must be' '$HOME/promote/log'"
expect "root above the domains refused" "grep -q 'REJECT docs d6.*root must be' '$HOME/promote/log'"
expect "site inside a bad root blocked" "grep -q 'REJECT docs d10.*overlaps' '$HOME/promote/log'"
expect "nothing deleted by it" "[ -f '$HOME/domains/docs.test/public_html/index.html' ] && [ -d '$HOME/domains/reports.test/public_html/app' ]"
sed -i '/^outside=/d; /^wide=/d' "$HOME/promote/config.env"

# Two sites whose roots nest are both refused: a publish of one would delete the other.
# shellcheck disable=SC2016 # $HOME is expanded later, when promote.sh reads the file
sed -i 's|^notes=.*|notes=$HOME/domains/docs.test/public_html/notes|' "$HOME/promote/config.env"
drop docs d8 site index.html
drop docs d9 notes index.html
bash "$here/../host/promote.sh"
expect "overlapping site refused (outer)" "grep -q 'REJECT docs d8.*overlaps' '$HOME/promote/log'"
expect "overlapping site refused (inner)" "grep -q 'REJECT docs d9.*overlaps' '$HOME/promote/log'"

# A held lock means another run is active: this run does nothing.
mkdir "$HOME/promote/lock"
drop mirror m3 iso x.iso
bash "$here/../host/promote.sh"
expect "held lock respected" "[ -f '$HOME/ci-drop/mirror/m3.ready' ]"
rmdir "$HOME/promote/lock"

if [ "$fails" -ne 0 ]; then echo "$fails failed"; exit 1; fi
echo "all passed"

#!/bin/bash
# Moves finished CI uploads from the write-only drop (~/ci-drop) into the web roots.
# Runs from cron every 5 minutes; each run is short (moves, not copies).
#
# Upload protocol (see .github/actions/publish/publish.sh):
#   <drop>/<channel>/<id>/...      the payload, written first
#   <drop>/<channel>/<id>.ready    one line: the destination path, written last
# Channels: reports, mirror, pkg, docs, and landing (a site's root index.html).
# Nothing is promoted until its .ready file exists. A payload that breaks a rule is moved
# to <state>/rejected/ and logged; it never reaches a web root.
set -euo pipefail
umask 022

CONF="${PROMOTE_CONF:-$HOME/promote/config.env}"
# shellcheck source=/dev/null
. "$CONF"
: "${DROP:?}" "${REPORTS_ROOT:?}" "${MIRROR_ROOT:?}" "${PKG_ROOT:?}"
DOCS_SITES="${DOCS_SITES:-}"
STATE="${STATE:-$HOME/promote}"
LOG="$STATE/log"
# rrsync can't create a channel directory itself.
mkdir -p "$STATE/rejected" "$DROP/landing"

lock="$STATE/lock"
if ! mkdir "$lock" 2>/dev/null; then
  # A run killed by the host leaves the lock behind; treat it as stale after two hours.
  [ -n "$(find "$lock" -maxdepth 0 -mmin +120)" ] || exit 0
  rmdir "$lock"
  mkdir "$lock"
fi
trap 'rmdir "$lock"' EXIT

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG"; }

# Anything Apache or PHP-FPM could execute, in any dotted segment of the name
# ("x.php.png" runs as PHP under AddHandler).
bad_type_re='\.(php[0-9]*|phtml|phar|pht|phps|cgi|fcgi|pl|py|rb|sh|shtml|asp|aspx|jsp)(\.|$)'
name_re='^[A-Za-z0-9._+~@=,%-]+$'
dest_re='^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$'

check_payload() {
  local dir=$1 p rel c lc
  local -a parts
  [ -d "$dir" ] || { echo "payload missing"; return 1; }
  if [ -n "$(find "$dir" ! -type f ! -type d -print -quit)" ]; then
    echo "symlink or special file"; return 1
  fi
  while IFS= read -r -d '' p; do
    rel=${p#"$dir"/}
    IFS=/ read -r -a parts <<<"$rel"
    for c in "${parts[@]}"; do
      case $c in
        .well-known) continue ;;
        .*) echo "dotfile: $rel"; return 1 ;;
      esac
      lc=${c,,}
      # Everything in cgi-bin/ runs as a program whatever its name (ScriptAlias).
      if [[ $lc =~ $bad_type_re || $lc == user.ini || $lc == php.ini || $lc == cgi-bin ]]; then
        echo "server-side type: $rel"; return 1
      fi
      [[ $c =~ $name_re ]] || { echo "unexpected characters: $rel"; return 1; }
    done
  done < <(find "$dir" -mindepth 1 -print0)
}

# Move every file under $1 to the same path under $2. A move within one filesystem is
# atomic per file, so a half-written ISO is never served. Index files go last (rank).
move_tree() {
  local src=$1 dst=$2 f
  mkdir -p "$dst"
  (cd "$src" && find . -mindepth 1 -type d -print0) | while IFS= read -r -d '' f; do
    mkdir -p "$dst/${f#./}"
  done
  (cd "$src" && find . -type f -print0) | while IFS= read -r -d '' f; do
    f=${f#./}
    case $f in
      index.yaml | */index.yaml | simple/* | state.json) printf '3 %s\0' "$f" ;;
      dists/*/Release | dists/*/InRelease | dists/*/Release.gpg) printf '2 %s\0' "$f" ;;
      dists/*) printf '1 %s\0' "$f" ;;
      *) printf '0 %s\0' "$f" ;;
    esac
  done | sort -z -n | while IFS= read -r -d '' f; do
    f=${f#* }
    mv -f "$src/$f" "$dst/$f"
  done
}

# Prints the web root of a docs site listed in DOCS_SITES ("name=/abs/path" per line).
# A docs publish deletes whatever the site no longer ships, so the root must be a dedicated
# directory: a domain's public_html or a folder in it, and not inside (or around) another
# site's root. (attackmap-docs once ran rsync --delete against a whole account through an
# empty path; nothing from CI decides a path here.)
docs_root() {
  local want=$1 line name root found="" other
  while IFS= read -r line; do
    line=${line%%#*}
    line=$(printf '%s' "$line" | tr -d '[:space:]')
    [ -n "$line" ] || continue
    name=${line%%=*}
    root=${line#*=}
    [ "$name" = "$want" ] && found=$root
  done <<<"$DOCS_SITES"
  [ -n "$found" ] || { echo "docs site '$want' is not listed in DOCS_SITES"; return 1; }
  found=${found%/}
  case $found in
    "$HOME"/domains/?*/public_html | "$HOME"/domains/?*/public_html/?*) ;;
    *) echo "docs site '$want': root must be ~/domains/<domain>/public_html or a folder in it"; return 1 ;;
  esac
  [[ $found != *..* ]] || { echo "docs site '$want': '..' in root"; return 1; }
  while IFS= read -r line; do
    line=${line%%#*}
    line=$(printf '%s' "$line" | tr -d '[:space:]')
    [ -n "$line" ] || continue
    [ "${line%%=*}" != "$want" ] || continue
    other=${line#*=}
    other=${other%/}
    if [ "$other" = "$found" ] || [[ $other == "$found"/* || $found == "$other"/* ]]; then
      echo "docs site '$want' overlaps site '${line%%=*}'"; return 1
    fi
  done <<<"$DOCS_SITES"
  printf '%s\n' "$found"
}

# The reports pages' style; ci/landing.py uses the same palette for mirror and pkg.
PAGE_STYLE=':root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--accent:#0b6bcb}
@media (prefers-color-scheme:dark){:root{--bg:#151517;--fg:#e8e8ea;--muted:#9a9aa0;--line:#2c2c30;--accent:#6cb2ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:960px;margin:0 auto;padding:40px 16px 64px}h1{font-size:28px;margin:0 0 4px}
p.lede{color:var(--muted);margin:0 0 24px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.table{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}'

page_head() { # title
  printf '<!doctype html><html lang="en"><meta charset="utf-8">'
  printf '<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s</title>\n' "$1"
  printf '<style>%s</style><main>\n' "$PAGE_STYLE"
}

html_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'; }

# Rewrites the commit list of <base>/<repo> and the front page of <base>. Security reports live at the
# root; leak-scan reports in leaks/, with its own front page linked from the root's.
write_reports_index() {
  local base=$1 repo=$2 dir="$1/$2" t name when summary
  {
    page_head "$repo reports"
    printf '<h1>%s</h1><p class="lede"><a href="../">all repositories</a> | <a href="latest/">latest</a></p>\n' "$repo"
    printf '<div class="table"><table>\n'
    printf '<tr><th>commit</th><th>published (UTC)</th><th>summary</th></tr>\n'
    find "$dir" -mindepth 1 -maxdepth 1 -type d ! -name latest -printf '%T@ %f\n' | sort -rn |
      while read -r t name; do
        when=$(date -u -d "@${t%.*}" '+%F %R')
        summary=$(head -c 300 "$dir/$name/summary.txt" 2>/dev/null | head -n1 | html_escape || true)
        printf '<tr><td><a href="%s/">%s</a></td><td>%s</td><td>%s</td></tr>\n' "$name" "${name:0:12}" "$when" "$summary"
      done
    printf '</table></div></main>\n'
  } >"$dir/index.html.new"
  mv -f "$dir/index.html.new" "$dir/index.html"
  write_front "$base"
}

write_front() {
  local base=$1 name when summary title lede
  if [ "$base" = "$REPORTS_ROOT" ]; then
    title="Security reports"
    lede="Trivy and Semgrep results for each repository. Counts are critical, high, medium and low."
  else
    title="Leak scan"
    lede="Secrets and private details found in each repository's files and history (values redacted)."
  fi
  {
    page_head "$title"
    printf '<h1>%s</h1><p class="lede">%s</p>\n' "$title" "$lede"
    if [ "$base" = "$REPORTS_ROOT" ] && [ -d "$REPORTS_ROOT/leaks" ]; then
      printf '<p><a href="leaks/">Leak scan</a></p>\n'
    elif [ "$base" != "$REPORTS_ROOT" ]; then
      printf '<p><a href="../">Security reports</a></p>\n'
    fi
    printf '<div class="table"><table><tr><th>repository</th><th>latest (UTC)</th><th>summary</th></tr>\n'
    # Only report folders: not the host's own (cgi-bin, .well-known and the like), nor leaks/.
    find "$base" -mindepth 1 -maxdepth 1 -type d ! -name cgi-bin ! -name '.*' ! -name leaks -printf '%f\n' | sort -f |
      while read -r name; do
        when="" summary=""
        if [ -d "$base/$name/latest" ]; then
          when=$(date -u -r "$base/$name/latest" '+%F %R')
          summary=$(head -c 300 "$base/$name/latest/summary.txt" 2>/dev/null | head -n1 | html_escape || true)
        fi
        printf '<tr><td><a href="%s/">%s</a></td><td>%s</td><td>%s</td></tr>\n' "$name" "$name" "$when" "$summary"
      done
    printf '</table></div></main>\n'
  } >"$base/index.html.new"
  mv -f "$base/index.html.new" "$base/index.html"
}

promote() {
  local ch=$1 dest=$2 payload=$3 repo target root
  case $ch in
    reports)
      base=$REPORTS_ROOT
      if [[ $dest == leaks/* ]]; then
        base=$REPORTS_ROOT/leaks
        dest=${dest#leaks/}
      fi
      [[ $dest =~ ^[A-Za-z0-9._-]+/[0-9a-f]{7,40}$ ]] || { echo "reports dest must be [leaks/]<repo>/<sha>"; return 1; }
      repo=${dest%/*}
      target="$base/$dest"
      mkdir -p "$base/$repo"
      rm -rf "$target.old"
      [ ! -e "$target" ] || mv "$target" "$target.old"
      mv "$payload" "$target"
      rm -rf "$target.old" "$base/$repo/latest.new"
      cp -R "$target" "$base/$repo/latest.new"
      rm -rf "$base/$repo/latest"
      mv "$base/$repo/latest.new" "$base/$repo/latest"
      write_reports_index "$base" "$repo"
      # The root's front page links leaks/ once it exists.
      [ "$base" = "$REPORTS_ROOT" ] || write_front "$REPORTS_ROOT"
      ;;
    mirror)
      # Additive: files are added or replaced, never deleted here.
      move_tree "$payload" "$MIRROR_ROOT/$dest"
      ;;
    pkg)
      case $dest in apt | helm | pypi) ;; *) echo "pkg dest must be apt, helm or pypi"; return 1 ;; esac
      move_tree "$payload" "$PKG_ROOT/$dest"
      ;;
    landing)
      # The root page of mirror or pkg. Reports writes its own (write_reports_index).
      case $dest in
        mirror) root=$MIRROR_ROOT ;;
        pkg) root=$PKG_ROOT ;;
        *) echo "landing dest must be mirror or pkg"; return 1 ;;
      esac
      [ "$(cd "$payload" && find . -mindepth 1 -printf '%P\n')" = index.html ] ||
        { echo "landing payload must be index.html alone"; return 1; }
      mv -f "$payload/index.html" "$root/index.html"
      ;;
    docs)
      [[ $dest != */* ]] || { echo "docs dest must be one site name"; return 1; }
      root=$(docs_root "$dest") || { echo "$root"; return 1; }
      # A site is replaced whole. The operator's files and the host's are never touched.
      mkdir -p "$root"
      rsync -a --delete --exclude='/.htaccess' --exclude='/.htpasswd*' --exclude='/.user.ini' \
        --exclude='/.well-known/acme-challenge/' --exclude='/cgi-bin/' "$payload/" "$root/"
      ;;
  esac
}

reject() {
  local ch=$1 id=$2 why=$3 to="$STATE/rejected/$1-$2"
  mkdir -p "$to"
  [ ! -e "$DROP/$ch/$id" ] || mv "$DROP/$ch/$id" "$to/payload"
  mv -f "$DROP/$ch/$id.ready" "$to/ready"
  log "REJECT $ch $id: $why"
}

for ch in reports mirror pkg docs landing; do
  [ -d "$DROP/$ch" ] || continue
  for ready in "$DROP/$ch"/*.ready; do
    [ -f "$ready" ] || continue
    id=$(basename "$ready" .ready)
    if ! [[ $id =~ ^[A-Za-z0-9._-]+$ ]]; then
      rm -f "$ready"; log "REJECT $ch: bad id"; continue
    fi
    dest=$(head -n1 "$ready" | tr -d '\r')
    if ! [[ $dest =~ $dest_re ]]; then
      reject "$ch" "$id" "bad destination"; continue
    fi
    if ! why=$(check_payload "$DROP/$ch/$id"); then
      reject "$ch" "$id" "$why"; continue
    fi
    if why=$(promote "$ch" "$dest" "$DROP/$ch/$id" 2>&1); then
      rm -rf "${DROP:?}/$ch/$id" "$ready"
      log "OK $ch $id -> $dest"
    else
      reject "$ch" "$id" "promote failed: $why"
    fi
  done
  # Uploads that never finished (no .ready after two days).
  find "$DROP/$ch" -mindepth 1 -maxdepth 1 -type d -mmin +2880 -print0 |
    while IFS= read -r -d '' d; do
      [ -e "$d.ready" ] || { rm -rf "$d"; log "PRUNE $ch $(basename "$d") (never finished)"; }
    done
done

find "$STATE/rejected" -mindepth 1 -maxdepth 1 -mtime +14 -exec rm -rf {} +

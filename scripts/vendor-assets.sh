#!/usr/bin/env bash
# Manage the vendored API-docs bundles (Swagger UI + ReDoc) in src/inkcast/static/vendor/.
#
#   scripts/vendor-assets.sh                 verify what is committed        (offline, instant)
#   scripts/vendor-assets.sh --outdated      is there a newer release?       (asks npm)
#   scripts/vendor-assets.sh --update        re-fetch the pinned versions    (repair)
#   scripts/vendor-assets.sh --upgrade       move to the latest and re-pin   (the 2-monthly job)
#
# ---------------------------------------------------------------------------------------
# WHY THE FILES ARE COMMITTED, instead of curl'd during the container build
# ---------------------------------------------------------------------------------------
# A build-time download makes every release depend on npm being up, on the exact version
# still being published (npm permits unpublish), and on the build host having a route out.
# Each of those turns a release into an incident at the moment you can least absorb one.
# Vendoring inverts it: the repository holds everything the build needs, so a build works
# from a checkout alone — offline, in a locked-down network, years later. That is the
# standard answer for air-gapped and supply-chain-sensitive builds. The cost is ~2.7MB in
# git and this script.
#
# ---------------------------------------------------------------------------------------
# WHY npm AND NOT THE CDN — the part worth understanding before you run --upgrade
# ---------------------------------------------------------------------------------------
# The obvious way to pin a file is to download it and hash it:
#
#     curl -sL https://cdn.jsdelivr.net/npm/redoc@2.5.0/... | sha256sum
#
# That is TRUST ON FIRST USE. The hash proves only that the bytes have not changed since
# YOU fetched them. If the CDN had served a tampered file, you would pin the tampered hash
# and every future build would verify it happily, forever — a pin that looks like diligence
# without being it.
#
# So this script never asks a CDN. It goes to the publisher:
#
#   1. GET registry.npmjs.org/<pkg>/<version>  ->  .dist.integrity   (sha512, base64)
#   2. download the release tarball, recompute sha512, compare to that digest
#   3. only then extract the file and record its sha256
#
# What lands in git is the publisher's bytes. Residual trust, stated plainly rather than
# glossed: the npm registry itself and TLS to it. Smaller and more accountable than a CDN
# edge, but not zero — mirror the tarballs to your own artifact store if you need better.

set -euo pipefail
cd "$(dirname "$0")/.."

VENDOR="src/inkcast/static/vendor"
LOCK="$VENDOR/ASSETS.lock"

# Which file we take out of which package. Versions are NOT here — they live in the lock,
# so --upgrade can rewrite them and git records the bump as a one-line diff.
#   package : path-inside-tarball : filename we serve
ASSETS=(
  # API-docs renderers served at /api (Swagger UI) and /api/redoc
  "redoc:package/bundles/redoc.standalone.js:redoc.standalone.js"
  "swagger-ui-dist:package/swagger-ui-bundle.js:swagger-ui-bundle.js"
  "swagger-ui-dist:package/swagger-ui.css:swagger-ui.css"
)

# sha256sum on Linux, shasum on macOS. Neither is present on both, and this script is run
# on a laptop and in CI.
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | awk '{print $1}'; }
  sha512hex() { sha512sum "$1" | awk '{print $1}'; }
else
  sha256() { shasum -a 256 "$1" | awk '{print $1}'; }
  sha512hex() { shasum -a 512 "$1" | awk '{print $1}'; }
fi

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

pinned_version() {  # package -> version recorded in the lock
  grep "^# version $1 " "$LOCK" 2>/dev/null | awk '{print $4}' | head -1
}

latest_version() {  # package -> npm's current `latest` dist-tag
  curl -sSfL "https://registry.npmjs.org/$1/latest" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'
}

# Fetch one package tarball into $WORK and verify it against npm's published integrity.
# Every network path in this script goes through here, so the check cannot be skipped.
fetch_and_verify_tarball() {
  # Three statements, not one `local a=.. b=.. c="$a$b"`. Bash expands every word on a
  # `local` line before performing any of its assignments, so the third would read an
  # unbound `$version` and `set -u` aborts.
  local pkg="$1"
  local version="$2"
  # The scope's slash cannot appear in a temp filename.
  local tgz="$WORK/${pkg//\//_}-$version.tgz"
  [ -f "$tgz" ] && return 0

  local integrity calc
  integrity=$(curl -sSfL "https://registry.npmjs.org/$pkg/$version" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["dist"]["integrity"])')
  # Scoped packages live at /@scope/name/-/name-<version>.tgz — the scope is dropped from
  # the filename, so basename is required or every scoped-package fetch 404s.
  curl -sSfL -o "$tgz" \
    "https://registry.npmjs.org/$pkg/-/$(basename "$pkg")-$version.tgz"

  # npm publishes sha512 as base64; our digest is hex, so convert before comparing.
  calc="sha512-$(sha512hex "$tgz" | xxd -r -p | base64 | tr -d '\n')"
  if [ "$integrity" != "$calc" ]; then
    bad "$pkg@$version FAILS npm's published integrity — refusing to vendor it"
    echo "      npm published: $integrity" >&2
    echo "      we computed  : $calc" >&2
    return 1
  fi
  ok "$pkg@$version tarball matches npm's published integrity"
  tar xzf "$tgz" -C "$WORK"
}

write_lock() {  # $1 = file holding "<sha256>  <name>" lines; versions from $VERSIONS
  {
    echo "# Vendored API-docs assets — generated by scripts/vendor-assets.sh, do not hand-edit."
    echo "#"
    echo "# Verified against npm's published \`integrity\` digest at fetch time, not against"
    echo "# a CDN download. See the header of vendor-assets.sh for why that distinction"
    echo "# matters. Re-check offline any time with: scripts/vendor-assets.sh"
    echo "#"
    for pkg in "${!VERSIONS[@]}"; do
      echo "# version $pkg ${VERSIONS[$pkg]}"
    done | sort
    echo "#"
    sort "$1"
  } > "$LOCK"
}

# =======================================================================================
# verify — the default. Offline, so it runs in CI, in a test, and on an air-gapped host.
# =======================================================================================
cmd_verify() {
  [ -f "$LOCK" ] || {
    bad "$LOCK is missing"
    echo "      Run: scripts/vendor-assets.sh --upgrade" >&2
    exit 1
  }
  bold "Verifying committed assets against $LOCK (no network)"
  local fail=0
  while read -r want file; do
    case "${want:-}" in ''|'#'*) continue ;; esac
    if [ ! -f "$VENDOR/$file" ]; then
      bad "$file is missing"; fail=1; continue
    fi
    local got; got=$(sha256 "$VENDOR/$file")
    if [ "$got" = "$want" ]; then
      ok "$file"
    else
      bad "$file has been modified"
      echo "      locked: $want" >&2
      echo "      actual: $got" >&2
      fail=1
    fi
  done < "$LOCK"

  if [ "$fail" -ne 0 ]; then
    echo >&2
    echo "Vendored assets do not match the lock." >&2
    echo "  If you changed them on purpose:  scripts/vendor-assets.sh --upgrade" >&2
    echo "  If you did not:                  git checkout -- $VENDOR" >&2
    exit 1
  fi
  echo
  echo "All assets match. Nothing was downloaded."
}

# =======================================================================================
# outdated — the two-monthly question, answered without changing anything
# =======================================================================================
cmd_outdated() {
  bold "Checking npm for newer releases"
  local behind=0 seen="" pkg cur new
  for entry in "${ASSETS[@]}"; do
    IFS=: read -r pkg _ _ <<<"$entry"
    case " $seen " in *" $pkg "*) continue ;; esac
    seen="$seen $pkg"
    cur=$(pinned_version "$pkg"); new=$(latest_version "$pkg")
    if [ "$cur" = "$new" ]; then
      ok "$pkg $cur is current"
    else
      printf '  \033[33m↑\033[0m %s %s → %s available\n' "$pkg" "$cur" "$new"
      behind=1
    fi
  done
  echo
  if [ "$behind" -eq 0 ]; then
    echo "Everything is current. Nothing to do."
  else
    echo "To take the newer releases:"
    echo "    scripts/vendor-assets.sh --upgrade"
    echo "  then rebuild, and commit $VENDOR with the lock."
  fi
}

# =======================================================================================
# update / upgrade — the only paths that touch the network or the files
# =======================================================================================
cmd_fetch() {   # $1 = "pinned" (repair) | "latest" (bump)
  local which="$1"
  WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
  mkdir -p "$VENDOR"
  declare -gA VERSIONS=()
  : > "$WORK/lock"

  step "1. Resolving versions"
  local seen="" pkg v
  for entry in "${ASSETS[@]}"; do
    IFS=: read -r pkg _ _ <<<"$entry"
    case " $seen " in *" $pkg "*) continue ;; esac
    seen="$seen $pkg"
    if [ "$which" = "latest" ]; then
      v=$(latest_version "$pkg")
      local old; old=$(pinned_version "$pkg" || true)
      if [ -n "$old" ] && [ "$old" != "$v" ]; then
        ok "$pkg $old → $v"
      else
        ok "$pkg $v"
      fi
    else
      v=$(pinned_version "$pkg")
      [ -n "$v" ] || { bad "no version pinned for $pkg; use --upgrade"; exit 1; }
      ok "$pkg $v (pinned)"
    fi
    VERSIONS["$pkg"]="$v"
  done

  step "2. Verifying tarballs against npm's published integrity"
  for pkg in "${!VERSIONS[@]}"; do
    fetch_and_verify_tarball "$pkg" "${VERSIONS[$pkg]}" || exit 1
  done

  step "3. Extracting and recording sha256"
  local path dest before after
  for entry in "${ASSETS[@]}"; do
    IFS=: read -r pkg path dest <<<"$entry"
    before=""; [ -f "$VENDOR/$dest" ] && before=$(sha256 "$VENDOR/$dest")
    cp "$WORK/$path" "$VENDOR/$dest"
    after=$(sha256 "$VENDOR/$dest")
    if [ "$before" = "$after" ]; then
      ok "$dest unchanged ($(( $(wc -c < "$VENDOR/$dest") / 1024 )) KB)"
    else
      printf '  \033[33m~\033[0m %s updated (%s KB)\n' \
        "$dest" "$(( $(wc -c < "$VENDOR/$dest") / 1024 ))"
    fi
    echo "$after  $dest" >> "$WORK/lock"
  done

  write_lock "$WORK/lock"

  step "Done"
  echo "Wrote $LOCK."
  echo
  echo "Next:"
  echo "  1. scripts/vendor-assets.sh                      re-check offline"
  echo "  2. git add $VENDOR && git status"
  echo "  3. python -m unittest tests.test_vendored_assets, then confirm /api and /api/redoc render"
  echo
  echo "The bundles are package data, so the wheel carries them — the container build"
  echo "downloads nothing."
}

case "${1:-}" in
  ""|--verify)   cmd_verify ;;
  --outdated)    cmd_outdated ;;
  --update)      cmd_fetch pinned ;;
  --upgrade)     cmd_fetch latest ;;
  -h|--help)     sed -n '2,8p' "$0" ;;
  *)             echo "unknown argument: $1" >&2; sed -n '2,8p' "$0" >&2; exit 2 ;;
esac

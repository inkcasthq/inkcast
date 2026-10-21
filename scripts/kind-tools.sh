#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# Install the pinned kind and kubectl (scripts/versions.env) into a bin directory, each checked against its pinned
# SHA-256 (E1.F1.8.S4, issue #37). Used by CI's kind job and by scripts/dev/stack.sh.
#
#   scripts/kind-tools.sh [bin-dir]     # default ~/.local/bin; prints the directory to put first on PATH
#
# A tool already there at the pinned version is kept. A checksum not pinned yet (empty in versions.env) is printed
# and the script fails, so a new pin is always read from the download once and then committed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/versions.env"
BIN="${1:-$HOME/.local/bin}"
mkdir -p "$BIN"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac

sha256() { (command -v sha256sum >/dev/null && sha256sum "$1" || shasum -a 256 "$1") | cut -d' ' -f1; }

fetch() {  # name version url pinned-sha
  local name="$1" version="$2" url="$3" want="$4" tmp
  if [ -x "$BIN/$name" ] && [ -f "$BIN/.$name-version" ] && [ "$(cat "$BIN/.$name-version")" = "$version" ]; then
    return 0
  fi
  tmp="$(mktemp)"
  curl -fsSL --retry 3 -o "$tmp" "$url"
  local got; got="$(sha256 "$tmp")"
  if [ -z "$want" ]; then
    rm -f "$tmp"
    echo "::error title=${name} ${version} not pinned::SHA-256 for ${os}/${arch} is ${got}; add it to scripts/versions.env"
    echo "$name $version ($os/$arch): no pinned SHA-256 in scripts/versions.env; the download's is $got" >&2
    exit 1
  fi
  if [ "$got" != "$want" ]; then
    rm -f "$tmp"
    echo "$name $version: SHA-256 $got is not the pinned $want; not installed" >&2
    exit 1
  fi
  install -m 0755 "$tmp" "$BIN/$name" && rm -f "$tmp"
  echo "$version" > "$BIN/.$name-version"
}

key="${os}_${arch}"
kind_sha="KIND_SHA256_${key}"; kubectl_sha="KUBECTL_SHA256_${key}"
fetch kind "$KIND_VERSION" "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-$os-$arch" "${!kind_sha:-}"
fetch kubectl "$KUBECTL_VERSION" "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch/kubectl" "${!kubectl_sha:-}"
echo "$BIN"

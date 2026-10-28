#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# The pull Secret for ghcr.io/inkcasthq/inkcast, a private package, in one of the lab's app namespaces (issue #296):
#
#   scripts/deploy/ghcr-pull-secret.sh <staging|prod> <github-username> <token-file>
#
# The token is a classic personal access token with read:packages only, saved to a file by the administrator; the
# command-line tool's own token (admin:org, repo, workflow, write:packages) is not it. It goes from the file to this
# shell to `oc` on stdin, never in an argument (nothing in `ps`), never on stdout or stderr. The Secret `ghcr-pull`
# (kubernetes.io/dockerconfigjson, for ghcr.io) is created, or replaced when it exists, so a rotated token is one
# more run. The environments' manifests name it on the ServiceAccount and on every pod spec that runs the image
# (environments/staging, environments/production); removing it once the package is public is in the README, Hosted §2.
set -euo pipefail
usage() { echo "usage: ghcr-pull-secret.sh <staging|prod> <github-username> <token-file>" >&2; exit 2; }
side="${1:-}"; user="${2:-}"; file="${3:-}"
case "$side" in
  staging|prod) ;;
  *) usage ;;
esac
[ -n "$user" ] && [ -n "$file" ] || usage
# A GitHub username: letters, digits and single hyphens, never a hyphen first or last (so never an option by mistake).
[[ "$user" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] \
  || { echo "'$user' is not a GitHub username (letters, digits, hyphens inside)." >&2; exit 2; }
[ -r "$file" ] || { echo "$file: cannot be read." >&2; exit 2; }
ns="inkcast-$side"
name=ghcr-pull

# The file holds the token and nothing else (a trailing newline is fine; a second line, a space or a stray
# character is not, and is reported without the value).
token="$(<"$file")"
[[ "$token" =~ ^[A-Za-z0-9_]+$ ]] || { echo "$file must hold one token (letters, digits, underscores) and nothing else." >&2; exit 2; }

# The namespace first, with the command that creates it; any other failure of oc (no cluster, no access) is its own.
if ! answer="$(oc get namespace "$ns" -o name 2>&1)"; then
  case "$answer" in
    *NotFound*) echo "Namespace $ns does not exist: oc new-project $ns first." >&2 ;;
    *) echo "$answer" >&2 ;;
  esac
  exit 1
fi
existing="$(oc -n "$ns" get secret "$name" --ignore-not-found -o name)" || exit 1
verb=create; done_word=Created
if [ -n "$existing" ]; then verb=replace; done_word=Replaced; fi

# What `oc create secret docker-registry` would write (username, password and their base64 `auth`), built here so
# the token travels on stdin. printf is a shell builtin: nothing of this appears in a process list.
auth="$(printf '%s' "$user:$token" | base64 | tr -d '\n')"
printf '%s\n' \
  'apiVersion: v1' \
  'kind: Secret' \
  'type: kubernetes.io/dockerconfigjson' \
  'metadata:' \
  "  name: $name" \
  '  labels:' \
  '    app.kubernetes.io/part-of: inkcast' \
  'stringData:' \
  "  .dockerconfigjson: '{\"auths\":{\"ghcr.io\":{\"username\":\"$user\",\"password\":\"$token\",\"auth\":\"$auth\"}}}'" \
  | oc -n "$ns" "$verb" -f - >/dev/null
echo "$done_word Secret $ns/$name (kubernetes.io/dockerconfigjson, ghcr.io, user $user)."

#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# One command for the whole walkthrough: walk -> integrity -> environment facts -> document.
#
#   scripts/dev/walkthrough/run_walk.sh --out /tmp/walkthrough
#
# The password comes from the environment only (WALK_PASSWORD), never an argument: argv is visible to every
# process on the host. Unset, one is generated for this run and forgotten with it. Ports are overridable:
# WALK_INKCAST_PORT (8787), WALK_FAKES_PORT (8999), WALK_METRICS_PORT (9090), WALK_CALLBACK_PORT (0: a free port).
# WALK_PYTHON names the interpreter. The default is the checkout's .venv/bin/python (the setup guide's environment,
# CONTRIBUTING.md), else python3 (CI, which installs into the runner's own); it needs inkcast's dependencies.
# Nothing is installed.
#
# Exit 0 only if every step and every integrity check passed. The document is built either way, so a failing
# walk still leaves the evidence of what failed; when the walk itself exits non-zero, the last lines of the
# server and fakes logs are printed last, so CI's annotation (the last 25 lines of this output) carries them.
# Output: NN-*.png, results.json, integrity.jsonl, env.json, walkthrough.html, walkthrough.pdf, summary.md, the
# process logs.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../../.." && pwd)"
py="${WALK_PYTHON:-}"
if [ -z "${py}" ]; then
  if [ -x "${repo}/.venv/bin/python" ]; then py="${repo}/.venv/bin/python"; else py=python3; fi
fi
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="$2"; shift 2 ;;
    *) echo "unknown argument: $1 (only --out DIR is accepted; the password is WALK_PASSWORD in the environment)" >&2; exit 2 ;;
  esac
done
[ -n "${out}" ] || { echo "--out DIR is required" >&2; exit 2; }
if [ -z "${WALK_PASSWORD:-}" ]; then
  WALK_PASSWORD="$("${py}" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  export WALK_PASSWORD
fi
# The walk's own imports need the checkout, not an installed copy belonging to another one; the tests set the
# same PYTHONPATH (src:tests). Prepended so a pip-installed inkcast does not shadow this checkout.
export PYTHONPATH="${repo}/src:${repo}/tests${PYTHONPATH:+:${PYTHONPATH}}"
mkdir -p "${out}"
rc=0
walk_rc=0

echo "== 1/5 the walk (MCP client sign-in, platforms, connect, publish, schedule, token, sync, insights, API docs)"
"${py}" "${here}/walk.py" --out "${out}" || { walk_rc=$?; rc=1; }

echo "== 2/5 integrity of what was left behind"
"${py}" "${here}/integrity_check.py" "${out}" || rc=1

echo "== 3/5 environment facts"
"${py}" "${here}/env_facts.py" > "${out}/env.json"

echo "== 4/5 the document"
"${py}" "${here}/build_doc.py" "${out}" "${out}/env.json"

echo "== 5/5 no credential in the document either"
"${py}" "${here}/integrity_check.py" "${out}" --leaks-only || rc=1
if [ "${walk_rc}" -ne 0 ]; then
  # The walk failed: its reason is in results.json, and the last lines of each process log go here, last, so they
  # are what CI's annotation keeps. The server's come last of all: they are the usual reason.
  echo "== the walk exited ${walk_rc}; the last lines of the process logs (see results.json for the failed step)"
  for log in fakes.log server.log; do
    echo "-- ${out}/${log}:"
    [ -f "${out}/${log}" ] && tail -n 10 "${out}/${log}" || echo "(missing)"
  done
fi
echo "walkthrough finished with rc=${rc}: ${out}/walkthrough.pdf"
exit "${rc}"

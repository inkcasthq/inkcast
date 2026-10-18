#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# Sign in to Inkcast through a real Keycloak on kind (E9.F9.1.S2): the realm from deploy/keycloak, Inkcast from the
# ci-oidc overlay, and scripts/dev/oidc_signin.py playing the browser inside the Inkcast pod.
#
#   scripts/dev/keycloak-e2e.sh            # needs a kind cluster with the inkcast:ci image loaded (the kind CI job)
#
# Checks, each one failing the script: the realm imported with its placeholders filled (no "${" left), a person
# with the inkcast-user role signs in end to end and reaches /api/v1/me as themselves, and a person without the
# role is refused by Inkcast after Keycloak accepted their password. Secrets are random per run, never committed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KC="$ROOT/deploy/keycloak"
S="$KC/.secrets"
k="${KUBECTL:-kubectl}"
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24), end="")'; }

mkdir -p "$S"
for name in admin-password inkcast-client-secret; do [ -s "$S/$name" ] || rand > "$S/$name"; done

echo "== Keycloak with the inkcast realm"
$k apply -k "$KC"
$k -n keycloak rollout status deploy/keycloak --timeout=420s
kcadm() { $k -n keycloak exec deploy/keycloak -- /opt/keycloak/bin/kcadm.sh "$@"; }
kcadm config credentials --server http://localhost:8080 --realm master --user admin --password "$(cat "$S/admin-password")"
redirects="$(kcadm get clients -r inkcast -q clientId=inkcast-identity --fields redirectUris,secret)"
echo "$redirects" | grep -q 'http://localhost:8787/auth/callback' || { echo "realm placeholders not filled: $redirects" >&2; exit 1; }
if echo "$redirects" | grep -q '\${'; then echo "a \${...} placeholder was left unfilled" >&2; exit 1; fi

echo "== Two people: one with inkcast-user, one without"
ok_pw="$(rand)A1!"; no_pw="$(rand)B2!"
for who in ada no-role; do
  kcadm create users -r inkcast -s "username=$who@example.com" -s "email=$who@example.com" -s emailVerified=true \
    -s enabled=true -s firstName="${who^}" -s lastName=Example >/dev/null
done
kcadm set-password -r inkcast --username ada@example.com --new-password "$ok_pw"
kcadm set-password -r inkcast --username no-role@example.com --new-password "$no_pw"
kcadm remove-roles -r inkcast --uusername no-role@example.com --rolename default-roles-inkcast

echo "== Inkcast signing in through Keycloak (ci-oidc)"
$k apply -k "$ROOT/deploy/kustomize/overlays/ci-oidc"
$k -n inkcast-oidc create secret generic inkcast-secrets --from-literal=secret-key="$(rand)$(rand)" \
  --from-file=oidc-client-secret="$S/inkcast-client-secret" --dry-run=client -o yaml | $k apply -f -
$k -n inkcast-oidc rollout restart deploy/inkcast
$k -n inkcast-oidc rollout status deploy/inkcast --timeout=180s

drive() {  # user password expect
  $k -n inkcast-oidc exec -i deploy/inkcast -- env KC_USER="$1" KC_PASSWORD="$2" KC_EXPECT="$3" python - \
    < "$ROOT/scripts/dev/oidc_signin.py"
}
echo "-- ada@example.com (has inkcast-user): must sign in"
drive ada@example.com "$ok_pw" allowed
echo "-- no-role@example.com (role removed): must be refused by Inkcast"
drive no-role@example.com "$no_pw" refused
echo "Keycloak sign-in end to end: PASS"

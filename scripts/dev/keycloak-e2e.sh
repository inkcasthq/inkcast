#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# Sign in to Inkcast through a real Keycloak on kind (E9.F9.1.S2): the realm from deploy/keycloak, Inkcast from the
# ci-oidc overlay (then split mode, ci-split-oidc, behind the Gateway), and scripts/dev/oidc_signin.py playing the
# browser inside the pod that answers http://localhost:8787 (the Inkcast pod, then the Gateway pod).
#
#   scripts/dev/keycloak-e2e.sh            # needs a kind cluster with the inkcast:ci image loaded (the kind CI job)
#
# Checks, each one failing the script: the realm imported with its placeholders filled (no "${" left), a person
# with the inkcast-user role signs in end to end and reaches /api/v1/me as themselves, and a person without the
# role is refused by Inkcast after Keycloak accepted their password, a newcomer signs up (registration, the emailed
# verification link from Mailpit, the terms) and arrives with the default role, and one who never verifies is
# held back. A person an administrator disables in Keycloak loses their OAuth token and personal access token within
# a minute (the user sync, E9.F9.5.S2) and signs in again once re-enabled. Secrets are random per run, never committed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KC="$ROOT/deploy/keycloak"
S="$KC/.secrets"
k="${KUBECTL:-kubectl}"
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24), end="")'; }

mkdir -p "$S"
for name in admin-password inkcast-client-secret inkcast-sync-secret smtp-password; do [ -s "$S/$name" ] || rand > "$S/$name"; done

echo "== Keycloak with the inkcast realm"
$k apply -k "$KC"
$k -n keycloak rollout status deploy/keycloak --timeout=420s
$k -n keycloak rollout status deploy/mailpit --timeout=180s
kcadm() { $k -n keycloak exec deploy/keycloak -- /opt/keycloak/bin/kcadm.sh "$@"; }
kcadm config credentials --server http://localhost:8080 --realm master --user admin --password "$(cat "$S/admin-password")"
redirects="$(kcadm get clients -r inkcast -q clientId=inkcast-identity --fields redirectUris,secret)"
echo "$redirects" | grep -q 'http://localhost:8787/auth/callback' || { echo "realm placeholders not filled: $redirects" >&2; exit 1; }
if echo "$redirects" | grep -q '\${'; then echo "a \${...} placeholder was left unfilled" >&2; exit 1; fi
sync_roles="$(kcadm get-roles -r inkcast --uusername service-account-inkcast-sync --cclientid realm-management --effective)"
for role in view-users view-events; do
  echo "$sync_roles" | grep -q "\"$role\"" || { echo "the inkcast-sync service account lacks $role: $sync_roles" >&2; exit 1; }
done

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
  --from-file=oidc-client-secret="$S/inkcast-client-secret" \
  --from-file=keycloak-sync-secret="$S/inkcast-sync-secret" --dry-run=client -o yaml | $k apply -f -
$k -n inkcast-oidc rollout restart deploy/inkcast
$k -n inkcast-oidc rollout status deploy/inkcast --timeout=180s

ns=inkcast-oidc; browser=deploy/inkcast   # where the driver runs: the pod that answers http://localhost:8787
drive() {  # user password expect [register]
  $k -n "$ns" exec -i "$browser" -- env KC_USER="$1" KC_PASSWORD="$2" KC_EXPECT="$3" KC_REGISTER="${4:-}" \
    KC_SAVE_TOKENS="${SAVE_TOKENS:-}" MAILPIT_URL=http://mailpit.keycloak.svc:8025 python - < "$ROOT/scripts/dev/oidc_signin.py"
}
echo "-- ada@example.com (has inkcast-user): must sign in"
SAVE_TOKENS=/tmp/ada.tokens drive ada@example.com "$ok_pw" allowed

echo "-- E9.F9.5.S2: an administrator disables ada in Keycloak: her OAuth token and PAT must stop within 60 s"
ada_id="$(kcadm get users -r inkcast -q username=ada@example.com --fields id --format csv --noquotes)"
kcadm update "users/$ada_id" -r inkcast -s enabled=false
$k -n "$ns" exec -i "$browser" -- python - <<'PY'
import sys, time, httpx2
tokens = open("/tmp/ada.tokens").read().split()
deadline, codes = time.time() + 60, []
while time.time() < deadline:
    codes = [httpx2.get("http://localhost:8787/api/v1/me", headers={"Authorization": f"Bearer {t}"}).status_code
             for t in tokens]
    if codes == [401, 401]:
        print(f"PASS: both tokens refused {60 - (deadline - time.time()):.0f} s after Keycloak disabled her")
        metrics = httpx2.get("http://localhost:9090/metrics").text
        line = [l for l in metrics.splitlines() if l.startswith("inkcast_identity_users_disabled_total")]
        print("   ", *line)
        sys.exit(0 if line else 1)
    time.sleep(2)
sys.exit(f"FAIL: still {codes} 60 s after Keycloak disabled her (expected [401, 401])")
PY
kcadm update "users/$ada_id" -r inkcast -s enabled=true
echo "-- ada re-enabled: she signs in again"
drive ada@example.com "$ok_pw" allowed
echo "-- no-role@example.com (role removed): must be refused by Inkcast"
drive no-role@example.com "$no_pw" refused
newcomer="new-$(date -u +%H%M%S)@example.com"
echo "-- $newcomer signs up (E9.F9.2.S1): register, verify the emailed link, accept the terms, arrive with the default role"
drive "$newcomer" "$(rand)C3!" allowed 1
echo "-- unverified-$newcomer signs up but never verifies: must not reach Inkcast"
drive "unverified-$newcomer" "$(rand)D4!" unverified 1

echo "== Split mode (#17): one Deployment per service, the Gateway in front, the same Keycloak"
$k apply -k "$ROOT/deploy/kustomize/overlays/ci-split-oidc"
$k -n inkcast-split-oidc create secret generic inkcast-secrets --from-literal=secret-key="$(rand)$(rand)" \
  --from-file=oidc-client-secret="$S/inkcast-client-secret" --dry-run=client -o yaml | $k apply -f -
for d in $($k -n inkcast-split-oidc get deploy -o name); do $k -n inkcast-split-oidc rollout restart "$d"; done
"$ROOT/scripts/ci-rollout.sh" inkcast-split-oidc 240s
ns=inkcast-split-oidc; browser=deploy/inkcast-gateway   # the Gateway answers localhost:8787 in split mode
echo "-- ada@example.com through the Gateway: must sign in"
drive ada@example.com "$ok_pw" allowed
echo "-- no-role@example.com through the Gateway: must be refused by Inkcast"
drive no-role@example.com "$no_pw" refused
echo "-- split-$newcomer signs up through the Gateway"
drive "split-$newcomer" "$(rand)E5!" allowed 1
echo "Keycloak sign-in end to end, single and split: PASS"

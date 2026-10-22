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
# a minute (the user sync, E9.F9.5.S2) and signs in again once re-enabled. Delete my account (E9.F9.5.S4): dee deletes
# her account through Inkcast's page and Keycloak's delete_account action (scripts/dev/account_delete.py), her tokens
# answer 401, her Keycloak user and her Inkcast rows are gone and bob is untouched; bob then deletes from Keycloak's
# side as the account console does, and the sync erases him from the DELETE_ACCOUNT event within 60 s. Secrets are
# random per run, never committed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KC="$ROOT/deploy/keycloak"
S="$KC/.secrets"
k="${KUBECTL:-kubectl}"
# Starts with a letter: token_urlsafe can begin with '-', which kcadm then reads as an option (issue #117).
rand() { python3 -c 'import secrets; print("k" + secrets.token_urlsafe(24), end="")'; }

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
kcadm get realms/inkcast | python3 -c '
import json, sys
realm = json.load(sys.stdin)
got = {"eventsExpiration": realm.get("eventsExpiration"),
       "adminEventsExpiration": (realm.get("attributes") or {}).get("adminEventsExpiration")}
print("event retention (s):", got)
sys.exit(0 if got == {"eventsExpiration": 604800, "adminEventsExpiration": "604800"} else "events must expire after 7 days")'
sync_roles="$(kcadm get-roles -r inkcast --uusername service-account-inkcast-sync --cclientid realm-management --effective)"
for role in view-users view-events; do
  echo "$sync_roles" | grep -q "\"$role\"" || { echo "the inkcast-sync service account lacks $role: $sync_roles" >&2; exit 1; }
done
echo "$sync_roles" | grep -q '"manage-users"' && { echo "the inkcast-sync service account must not manage users" >&2; exit 1; }
echo "-- E9.F9.5.S4: delete_account enabled, delete-account a default role, DELETE_ACCOUNT recorded, the client links back"
# Parsed as JSON and printed, not grepped: kcadm's formatting is not a contract, and under pipefail `kcadm | grep -q`
# fails at random when grep exits on its match and kcadm dies of SIGPIPE writing the rest.
kcadm get authentication/required-actions -r inkcast | python3 -c '
import json, sys
actions = {a.get("alias"): a for a in json.load(sys.stdin)}
got = actions.get("delete_account")
print("delete_account required action:", {k: (got or {}).get(k) for k in ("alias", "providerId", "enabled", "defaultAction")})
sys.exit(0 if got and got.get("enabled") is True else "the delete_account required action is not enabled")'
kcadm get roles/default-roles-inkcast/composites -r inkcast | python3 -c '
import json, sys
names = sorted(r.get("name") for r in json.load(sys.stdin))
print("default-roles-inkcast composites:", names)
sys.exit(0 if "delete-account" in names else "default-roles-inkcast lacks the account client'"'"'s delete-account role")'
kcadm get events/config -r inkcast | python3 -c '
import json, sys
types = json.load(sys.stdin).get("enabledEventTypes") or []
print("DELETE_ACCOUNT recorded:", "DELETE_ACCOUNT" in types)
sys.exit(0 if "DELETE_ACCOUNT" in types else "DELETE_ACCOUNT is not an enabled event type")'
kcadm get clients -r inkcast -q clientId=inkcast-identity | python3 -c '
import json, sys
base = [c.get("baseUrl") for c in json.load(sys.stdin)]
print("inkcast-identity baseUrl:", base)
sys.exit(0 if base == ["http://localhost:8787/account/return"] else "the inkcast-identity client'"'"'s baseUrl must be Inkcast'"'"'s /account/return")'

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
echo "-- Keycloak's Back to Application link (the client's baseUrl) without a pending deletion: a neutral page"
$k -n "$ns" exec -i "$browser" -- python - <<'PY'
import sys, httpx2
r = httpx2.get("http://localhost:8787/account/return")
ok = r.status_code == 200 and "back at Inkcast" in r.text and "delet" not in r.text.lower()
print("PASS: /account/return is neutral" if ok else f"FAIL: HTTP {r.status_code}: {r.text[:300]}")
sys.exit(0 if ok else 1)
PY

echo "== E9.F9.5.S4: Delete my account"
dee_pw="$(rand)F6!"; bob_pw="$(rand)G7!"
for who in dee bob; do
  kcadm create users -r inkcast -s "username=$who@example.com" -s "email=$who@example.com" -s emailVerified=true \
    -s enabled=true -s firstName="${who^}" -s lastName=Example >/dev/null
done
kcadm set-password -r inkcast --username dee@example.com --new-password "$dee_pw"
kcadm set-password -r inkcast --username bob@example.com --new-password "$bob_pw"
SAVE_TOKENS=/tmp/dee.tokens drive dee@example.com "$dee_pw" allowed
SAVE_TOKENS=/tmp/bob.tokens drive bob@example.com "$bob_pw" allowed
inkcast_id() {  # the Inkcast user id behind a saved token file
  $k -n "$ns" exec -i "$browser" -- python - "$1" <<'PY'
import sys, httpx2
token = open(sys.argv[1]).read().split()[0]
print(httpx2.get("http://localhost:8787/api/v1/me", headers={"Authorization": f"Bearer {token}"}).raise_for_status().json()["id"])
PY
}
rows_left() {  # every table erase_user covers, for one Inkcast user id, from the pod's database (read-only)
  $k -n "$ns" exec -i "$browser" -- python - "$1" <<'PY'
import sqlite3, sys
from inkcast.libs.db.erasure import USER_TABLES
c = sqlite3.connect("file:/data/inkcast.db?mode=ro", uri=True)
left = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE {cond}", {"user_id": sys.argv[1]}).fetchone()[0] for t, cond in USER_TABLES}
print({t: n for t, n in left.items() if n})
PY
}
dee_id="$(inkcast_id /tmp/dee.tokens)"; bob_id="$(inkcast_id /tmp/bob.tokens)"
echo "-- dee ($dee_id) deletes her account through Inkcast's page and Keycloak's delete_account action"
$k -n "$ns" exec -i "$browser" -- env KC_USER=dee@example.com KC_PASSWORD="$dee_pw" KC_TOKENS=/tmp/dee.tokens \
  python - < "$ROOT/scripts/dev/account_delete.py"
[ -z "$(kcadm get users -r inkcast -q username=dee@example.com --fields id --format csv --noquotes)" ] \
  || { echo "dee's Keycloak user still exists" >&2; exit 1; }
left="$(rows_left "$dee_id")"; [ "$left" = "{}" ] || { echo "dee's Inkcast rows are still there: $left" >&2; exit 1; }
echo "   Keycloak user gone, Inkcast rows gone"
bob_left="$(rows_left "$bob_id")"
echo "$bob_left" | grep -q "'users': 1" || { echo "bob's rows should be untouched: $bob_left" >&2; exit 1; }
[ -n "$(kcadm get users -r inkcast -q username=no-role@example.com --fields id --format csv --noquotes)" ] \
  || { echo "no-role's Keycloak user vanished" >&2; exit 1; }
echo "   bob and no-role untouched"
echo "-- bob ($bob_id) deletes from Keycloak's side, as the account console does: erased from the DELETE_ACCOUNT event within 60 s"
$k -n "$ns" exec -i "$browser" -- env KC_USER=bob@example.com KC_PASSWORD="$bob_pw" KC_MODE=console \
  KC_ISSUER=http://keycloak.keycloak.svc:8080/realms/inkcast python - < "$ROOT/scripts/dev/account_delete.py"
$k -n "$ns" exec -i "$browser" -- python - "$bob_id" <<'PY'
import sqlite3, sys, time, httpx2
from inkcast.libs.db.erasure import USER_TABLES
tokens = open("/tmp/bob.tokens").read().split()
deadline, codes, left = time.time() + 60, [], {}
while time.time() < deadline:
    codes = [httpx2.get("http://localhost:8787/api/v1/me", headers={"Authorization": f"Bearer {t}"}).status_code for t in tokens]
    c = sqlite3.connect("file:/data/inkcast.db?mode=ro", uri=True)
    left = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE {cond}", {"user_id": sys.argv[1]}).fetchone()[0] for t, cond in USER_TABLES}
    if codes == [401, 401] and not any(left.values()):
        print(f"PASS: bob's tokens refused and his rows gone {60 - (deadline - time.time()):.0f} s after Keycloak deleted him")
        metrics = httpx2.get("http://localhost:9090/metrics").text
        line = [ln for ln in metrics.splitlines() if ln.startswith("inkcast_identity_users_deleted_total")]
        print("   ", *line)
        sys.exit(0 if any('via="event"' in ln for ln in line) else "the via=event metric is missing")
    time.sleep(2)
sys.exit(f"FAIL: still {codes}, rows left {left} 60 s after Keycloak deleted bob")
PY

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

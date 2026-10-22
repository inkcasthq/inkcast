# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Keycloak user sync (E9.F9.5.S2, ADR-0008): a person disabled or deleted in Keycloak loses Inkcast too.

Keycloak stops a disabled person signing in again, but the OAuth tokens and personal access tokens Inkcast already
gave them would keep working until they expire. This worker closes that gap, in IdentityService's process, by
polling (the ADR-0008 option that needs no Keycloak plug-in, so it works air-gapped):

* every ``keycloak_sync_interval_seconds`` it reads the realm's admin events for USER updates and deletes since its
  cursor, and re-reads each such person from the admin API;
* in the same pass it reads the realm's user events of type DELETE_ACCOUNT since a cursor of its own, with the same
  look-back: a person who deleted their own account (through Inkcast's "Delete my account", E9.F9.5.S4, or
  Keycloak's account console) is erased from Inkcast (``Store.erase_user``, every table in one transaction) rather
  than merely cut off, once: an event seen again finds no user row and changes nothing;
* every ``keycloak_reconcile_seconds`` (and on start) it re-reads every Keycloak-backed person Inkcast knows, the
  cut-off ones included, so a USER event missed while it was down or trimmed by Keycloak's retention still leads
  to the same cut-off. A person who is gone is erased at once when Keycloak still holds their DELETE_ACCOUNT event
  (a late event, a burst, a deletion from the account console); when it holds none (an administrator deleted them,
  or retention trimmed the event while the sync was down) they are cut off and erased once they have been gone for
  ``keycloak_erase_after_days`` (E9.F9.5.S6: ``0`` at the first reconcile that finds them gone, ``None`` = ``off``
  never, cut off only). An account restored meanwhile under the same id stops the clock.

A person who is gone (404) or ``enabled: false`` is disabled in Inkcast: every OAuth token, code and personal
access token is cut off in one transaction (``Store.disable_user``) and the person is marked, which clears at their
next successful sign-in (Keycloak only allows that once they are enabled again). A disabled-but-present person is
never erased. The worker signs in to Keycloak as its own service account (client credentials; the realm's
``inkcast-sync`` client with realm-management ``view-users`` and ``view-events`` only). Errors never stop the loop:
they are logged, counted and retried. An answer about one person that the sync cannot act on (a 500 for that user,
a malformed id; ``PersonError``) is counted as ``inkcast_identity_sync_errors_total{stage="person"}``, logged with the
Inkcast user id only, and the pass goes on with the others (#103): that person is read again at the next pass or
reconcile. Anything else (Keycloak unreachable, the service account refused, the database) fails the pass as a
whole, and so does a reconcile that could act on nobody.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx2

log = logging.getLogger("inkcast.identity.sync")
CURSOR = "keycloak-admin-events"
USER_EVENTS_CURSOR = "keycloak-user-events"
# Events become visible out of time order (stamped when a request starts, stored when it commits; replicas have
# their own clocks), so each pass also re-reads the minute before each cursor (admin and user events alike).
# Re-checking a person is harmless, and re-seeing an erased one is a no-op.
LOOKBACK_MS = 60_000


class SyncError(Exception):
    """The pass fails: Keycloak refused the service account or a realm-wide read, or the reconcile could act on nobody."""


class PersonError(SyncError):
    """Keycloak answered an error about one person (``users/{id}``, or their events): that person's alone, and the pass
    goes on (#103). ``call`` names the read (``user``, ``events``) and ``status`` the HTTP status: a fixed string and
    an int, all a log line about them carries."""

    def __init__(self, message: str, call: str, status: int):
        super().__init__(message)
        self.call, self.status = call, status


def _user_not_found(r: httpx2.Response) -> bool:
    try:
        body = r.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "User not found"


def erase_person(store: Any, metrics: Any, user_id: str, via: str) -> dict[str, int]:
    """Erase one person whose account is gone (``via`` ``return`` for the visit back from Keycloak, ``event`` for
    the DELETE_ACCOUNT event pass, ``reconcile`` for the reconcile: a missed event, or the grace period over); logs
    ``identity.user.deleted`` with ids and counts only. Nothing to erase is not an error (another path got there
    first), and is neither logged nor counted."""
    counts = store.erase_user(user_id)
    if not any(counts.values()):
        return counts
    posts, tokens, pats = (int(counts.get(k, 0)) for k in ("posts", "tokens", "api_tokens"))
    log.warning("Inkcast user %s's account is gone (%s): erased %d posts, %d OAuth grants, %d PATs, %d rows in all",
                user_id, via, posts, tokens, pats, sum(counts.values()),
                extra={"event": "identity.user.deleted", "user_id": user_id, "via": via, "rows": sum(counts.values())})
    if metrics:
        metrics.inc("inkcast_identity_users_deleted_total", via=via)
    return counts


class KeycloakSync:
    def __init__(self, config: Any, store: Any, http: httpx2.AsyncClient, metrics: Any = None, clock=time.time):
        self.config, self.store, self.http, self.metrics, self.clock = config, store, http, metrics, clock
        issuer = config.oidc_issuer.rstrip("/")
        self.issuer = issuer
        self.admin = issuer.replace("/realms/", "/admin/realms/", 1)
        self.token_url = f"{issuer}/protocol/openid-connect/token"
        self.prefix = f"oidc:{config.oidc_issuer}#"      # exactly how login.py keys a Keycloak person (oidc:<iss>#<sub>)
        self._token: str | None = None
        self._token_until = 0.0
        self.last_success = 0.0
        self.last_reconcile = 0.0
        self.unread_until = 0.0     # after a reconcile that could act on nobody: no full re-read before this

    @property
    def enabled(self) -> bool:
        return bool(self.config.keycloak_sync_client_id)

    # ---- the admin API --------------------------------------------------------------------------------------

    async def _access_token(self) -> str:
        if self._token and self.clock() < self._token_until:
            return self._token
        r = await self.http.post(self.token_url, data={
            "grant_type": "client_credentials", "client_id": self.config.keycloak_sync_client_id,
            "client_secret": self.config.keycloak_sync_secret.reveal()}, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise SyncError(f"Keycloak refused the sync service account (HTTP {r.status_code})")
        body = r.json()
        self._token = body["access_token"]
        self._token_until = self.clock() + max(10.0, float(body.get("expires_in", 60)) - 30)
        return self._token

    async def _get(self, path: str, params: Any = None) -> httpx2.Response:
        for attempt in (1, 2):
            r = await self.http.get(f"{self.admin}/{path}", params=params,
                                    headers={"Authorization": f"Bearer {await self._access_token()}"})
            if r.status_code == 401 and attempt == 1:  # the token expired early or was revoked: once more
                self._token = None
                continue
            return r
        return r

    async def person(self, keycloak_id: str) -> str:
        """'active', 'disabled' or 'deleted', as Keycloak sees them now. Only Keycloak's own answer for a missing
        user (404 ``{"error": "User not found"}``, UsersResource) means 'deleted': a 404 from anything else (a proxy
        that doesn't expose /admin/, a wrong URL) is an error, never a reason to erase or cut anyone off."""
        r = await self._get(f"users/{quote(keycloak_id, safe='')}")   # one path segment: '#', '?' or '/' ask about another id
        if r.status_code == 404 and _user_not_found(r):
            return "deleted"
        if r.status_code != 200:
            raise PersonError(f"reading users/{keycloak_id} answered HTTP {r.status_code}", "user", r.status_code)
        return "active" if r.json().get("enabled", True) else "disabled"

    async def changed_people(self) -> list[str]:
        """Keycloak ids from USER update/delete admin events at or after LOOKBACK_MS before the cursor (re-checking
        a person is harmless); moves the cursor. Keycloak answers newest first, at
        most 500: a burst bigger than that between two passes is left to the reconcile."""
        cursor = int(self.store.get_sync_state(CURSOR) or self.clock() * 1000)
        day = datetime.fromtimestamp(cursor / 1000 - 86400, timezone.utc).strftime("%Y-%m-%d")
        r = await self._get("admin-events", params=[("resourceTypes", "USER"), ("operationTypes", "UPDATE"),
                                                    ("operationTypes", "DELETE"), ("dateFrom", day), ("first", "0"),
                                                    ("max", "500")])
        if r.status_code != 200:
            raise SyncError(f"reading admin-events answered HTTP {r.status_code}")
        newest, ids = cursor, []
        for event in r.json():
            at = int(event.get("time", 0))
            path = event.get("resourcePath", "")
            if at >= cursor - LOOKBACK_MS and path.startswith("users/"):
                ids.append(path.split("/")[1])
            newest = max(newest, at)
        self.store.set_sync_state(CURSOR, str(newest))
        return list(dict.fromkeys(ids))

    async def deleted_accounts(self) -> tuple[dict[str, int], int, int]:
        """Keycloak ids from DELETE_ACCOUNT user events at or after LOOKBACK_MS before their own cursor (the person
        deleted the account themselves; one already erased has no user row left, so seeing the event again erases,
        counts and records nothing), each with the earliest such event's time, newest person first; then the cursor
        as read and the newest event time. ``tick`` moves the cursor to the newest only once every one of them is
        erased, and no further than the event of a person it could not act on, so an erasure that failed is read
        again. Same shape as ``changed_people``: newest first, at most 500."""
        cursor = int(self.store.get_sync_state(USER_EVENTS_CURSOR) or self.clock() * 1000)
        day = datetime.fromtimestamp(cursor / 1000 - 86400, timezone.utc).strftime("%Y-%m-%d")
        r = await self._get("events", params=[("type", "DELETE_ACCOUNT"), ("dateFrom", day), ("first", "0"), ("max", "500")])
        if r.status_code != 200:
            raise SyncError(f"reading events answered HTTP {r.status_code}")
        newest, earliest = cursor, {}
        for event in r.json():
            at = int(event.get("time", 0))
            if at >= cursor - LOOKBACK_MS and event.get("type") == "DELETE_ACCOUNT" and event.get("userId"):
                keycloak_id = str(event["userId"])
                earliest[keycloak_id] = min(at, earliest.get(keycloak_id, at))
            newest = max(newest, at)
        return earliest, cursor, newest

    async def deleted_own_account(self, keycloak_id: str) -> bool:
        """Whether Keycloak holds a DELETE_ACCOUNT user event for this person, for the reconcile: a person gone from
        Keycloak who deleted the account themselves is erased even when the pass above missed the event (more than
        LOOKBACK_MS late, a replica's clock running fast, a burst of more than 500); one an administrator deleted,
        or whose event retention has trimmed, is cut off and erased after ``keycloak_erase_after_days``."""
        r = await self._get("events", params=[("type", "DELETE_ACCOUNT"), ("user", keycloak_id), ("first", "0"),
                                              ("max", "1")])
        if r.status_code != 200:
            raise PersonError(f"reading events answered HTTP {r.status_code}", "events", r.status_code)
        return any(e.get("type") == "DELETE_ACCOUNT" and e.get("userId") == keycloak_id for e in r.json())

    # ---- what Inkcast does about it ------------------------------------------------------------------------

    def _erase(self, keycloak_id: str, via: str) -> bool:
        user = self.store.find_user_by_identity(self.prefix + keycloak_id)
        if not user:
            return False
        return any(erase_person(self.store, self.metrics, str(user["id"]), via).values())

    def _cut_off(self, keycloak_id: str, state: str, via: str) -> bool:
        """Cut the person off once (tokens, codes, PATs; ``disabled_at``). A person Keycloak no longer has (state
        ``deleted``) is also marked gone from now, already cut off or not: the erasure grace period runs from the
        first time the sync finds them gone, on either path."""
        user = self.store.find_user_by_identity(self.prefix + keycloak_id)
        if not user:
            return False
        user_id = str(user["id"])
        if state == "deleted" and not user.get("deleted_at"):
            self.store.mark_user_deleted(user_id, self.clock())
        if user.get("disabled_at"):
            return False
        cut = self.store.disable_user(user_id)
        # Only the Inkcast id, a fixed reason and counts reach the log: nothing from Keycloak's answers or the
        # token tables (the identity key already ties the user id to the Keycloak id).
        reason = "deleted" if state == "deleted" else "disabled"
        oauth, pats, codes = (int(cut.get(k, 0)) for k in ("oauth_tokens", "personal_access_tokens", "codes"))
        log.warning("Keycloak %s the person behind Inkcast user %s: cut off %d OAuth grants, %d PATs, %d codes",
                    reason, user_id, oauth, pats, codes,
                    extra={"event": "identity.user.disabled", "user_id": user_id, "reason": reason, "via": via,
                           "revoked_oauth": oauth, "revoked_pats": pats, "revoked_codes": codes})
        if self.metrics:
            self.metrics.inc("inkcast_identity_users_disabled_total", reason=reason)
        return True

    def _grace_over(self, user_id: str, listed: dict[str, Any]) -> bool:
        """Whether a person Keycloak no longer has (``deleted_at`` set by ``_cut_off``) has been gone for
        ``keycloak_erase_after_days``: exactly that many days counts, ``0`` is the first reconcile that finds them
        gone, ``None`` (``off``) is never. Never when they signed in since ``listed`` was read: Keycloak let them in
        after the 404 this reconcile is acting on."""
        days = self.config.keycloak_erase_after_days
        user = self.store.get_user(user_id) if days is not None else None
        if not user or user.get("deleted_at") is None or user.get("last_login_at") != listed.get("last_login_at"):
            return False
        return self.clock() - float(user["deleted_at"]) >= days * 86400

    def _person_failed(self, user_id: str | None, via: str, exc: PersonError) -> None:
        """Keycloak answered about one person, but not something the sync can act on: counted and logged, and the
        pass goes on with the others (#103); the person is read again at the next pass or reconcile. Only the
        Inkcast user id (None for a person Inkcast does not know), which read it was and the HTTP status reach the
        log: never the Keycloak id, nor anything from the answer."""
        log.warning("Keycloak answered HTTP %d to the %s read about Inkcast user %s (%s): retried at the next read; "
                    "the others go on", exc.status, exc.call, user_id, via,
                    extra={"event": "identity.sync.person.failed", "user_id": user_id, "via": via, "call": exc.call,
                           "status": exc.status})
        if self.metrics:
            self.metrics.inc("inkcast_identity_sync_errors_total", stage="person")

    async def tick(self) -> dict[str, int]:
        """One pass: the events since the cursors, then a reconcile when it is due. Returns what it did. A
        ``PersonError`` is that person's alone (``_person_failed``); any other error, or a reconcile that could act
        on nobody (a realm-wide 403 or 500), fails the pass as a whole, as before."""
        done = {"checked": 0, "disabled": 0, "erased": 0}
        deleted, cursor, newest = await self.deleted_accounts()
        for keycloak_id, at in deleted.items():
            # The look-back reads an event again on later passes, and Keycloak can have the person again under the
            # same id (restored from a realm export, LDAP without import): erase only once it confirms they are gone.
            user = self.store.find_user_by_identity(self.prefix + keycloak_id)
            if not user:
                continue
            try:
                if await self.person(keycloak_id) == "deleted" and self._erase(keycloak_id, "event"):
                    done["erased"] += 1
            except PersonError as exc:
                self._person_failed(str(user["id"]), "event", exc)
                newest = min(newest, at)         # the cursor stops before their event: read again until erased
        self.store.set_sync_state(USER_EVENTS_CURSOR, str(max(cursor, newest)))
        for keycloak_id in await self.changed_people():
            done["checked"] += 1
            try:
                state = await self.person(keycloak_id)
            except PersonError as exc:                 # the reconcile re-reads everyone: like an event the pass missed
                known = self.store.find_user_by_identity(self.prefix + keycloak_id)
                self._person_failed(str(known["id"]) if known else None, "event", exc)
                continue
            if state != "deleted" and (known := self.store.find_user_by_identity(self.prefix + keycloak_id)):
                self.store.mark_user_present(str(known["id"]))     # Keycloak has them: a running grace period stops
            if state != "active" and self._cut_off(keycloak_id, state, "event"):
                done["disabled"] += 1
        if self.clock() - self.last_reconcile >= self.config.keycloak_reconcile_seconds:
            if self.clock() < self.unread_until:         # still failing, but everyone is read again on schedule
                raise SyncError("the last reconcile could act on nobody; everyone is read again at the next one")
            # The cut-off people too: one Keycloak no longer has is erased once the grace period is over; one it has
            # again (restored from a backup under the same id) gets a whole grace period at a later deletion.
            people = self.store.users_with_identity_prefix(self.prefix, include_disabled=True)
            unread = 0
            for user in people:
                keycloak_id, user_id = user["identity"][len(self.prefix):], str(user["id"])
                done["checked"] += 1
                try:
                    state = await self.person(keycloak_id)
                except PersonError as exc:               # read again at the next reconcile, on schedule
                    unread += 1
                    self._person_failed(user_id, "reconcile", exc)
                    continue
                if state != "deleted":                   # present, enabled or not: never erased
                    if user.get("deleted_at") is not None:
                        self.store.mark_user_present(user_id)
                    if state == "disabled" and self._cut_off(keycloak_id, state, "reconcile"):
                        done["disabled"] += 1
                    continue
                try:
                    own_deletion = await self.deleted_own_account(keycloak_id)
                except PersonError as exc:               # gone for certain: cut off now; the event, next reconcile
                    self._person_failed(user_id, "reconcile", exc)
                    own_deletion = False
                if own_deletion:
                    if self._erase(keycloak_id, "reconcile"):   # an event the pass missed: erased, as the pass would have
                        done["erased"] += 1
                else:                                    # an administrator's deletion, or the event is past retention
                    if self._cut_off(keycloak_id, state, "reconcile"):
                        done["disabled"] += 1
                    if self._grace_over(user_id, user) and self._erase(keycloak_id, "reconcile"):
                        done["erased"] += 1
            if unread and unread == len(people):         # nobody could be read: the sync is failing, not a person
                self.unread_until = self.clock() + self.config.keycloak_reconcile_seconds
                raise SyncError(f"the reconcile could act on nobody: every one of {unread} reads failed")
            self.last_reconcile = self.clock()
        self.last_success = self.clock()
        return done

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("Keycloak user sync on: %s every %.0f s, full reconcile every %.0f s", self.admin,
                 self.config.keycloak_sync_interval_seconds, self.config.keycloak_reconcile_seconds)
        while not stop.is_set():
            try:
                await self.tick()
                if self.metrics:
                    self.metrics.inc("inkcast_identity_sync_total", result="ok")
            except Exception as exc:  # any error (Keycloak, an odd answer, the database): logged, counted, retried
                log.error("Keycloak user sync failed: %s", exc, extra={"event": "identity.sync.failed"})
                if self.metrics:
                    self.metrics.inc("inkcast_identity_sync_total", result="error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.keycloak_sync_interval_seconds)
            except asyncio.TimeoutError:
                pass

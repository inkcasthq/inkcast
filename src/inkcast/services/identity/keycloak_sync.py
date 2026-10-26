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

Each of those acts is decided on a reading of the person that another replica may overtake before the transaction
starts, so the caller passes the row as it read it and the store re-checks it inside the transaction (issue #102:
``erase_user`` with the row, ``disable_user`` and ``mark_user_deleted`` with ``last_login_at``): a sign-in handled
elsewhere in between leaves the person as they are, and the next pass judges them afresh.

A person who is gone (404) or ``enabled: false`` is disabled in Inkcast: every OAuth token, code and personal
access token is cut off in one transaction (``Store.disable_user``) and the person is marked, which clears at their
next successful sign-in (Keycloak only allows that once they are enabled again). A disabled-but-present person is
never erased. The worker signs in to Keycloak as its own service account (client credentials; the realm's
``inkcast-sync`` client with realm-management ``view-users`` and ``view-events`` only). Errors never stop the loop:
they are logged, counted and retried. An answer about one person that the sync cannot act on (a 500 for that user,
a malformed id; ``PersonError``) is counted as ``inkcast_identity_sync_errors_total{stage="person"}``, logged with the
Inkcast user id only, and the pass goes on with the others (#103): that person is read again at the next pass or
reconcile. So is a read about one person that reached Keycloak and got no answer (a timeout, the connection closed
without an answer: one user whose read hangs on an LDAP referral must not block everyone after them, #121), and the
pass does not read them again. A pass has a budget of ``TIMEOUT_BUDGET`` such reads (#152): at the third it ends,
as a success for what it did (the DELETE_ACCOUNT cursor stops before every event not acted on; a reconcile acts on
whoever it read, but for what the breaker would hold while the people it left untried, not known to hang, could take
its count to the minimum; the next is on schedule), so that an outage that begins mid-pass costs it three timeouts, not
one per person, and two people who hang never block the rest. The reconcile reads the people whose last read got no
answer last, so they delay each other only, and in the pass that reconciles the event passes leave those people to
it, so that three of them with an event each cannot use the budget before it: everyone else is read first. A pass
none of whose reads about a person answered fails as a whole instead (the sync cannot tell an outage from a pass
that read only people who hang), and a reconcile that could read nobody, for that or any reason, sets
``unread_until`` (#119). A read outside a pass (the return page's) counts for no pass. A transport error on the way
to Keycloak (a connection refused or reset, a connect timeout, the client's own pool) is never one person's: it
fails the pass at once. Anything else (the service account refused or without an answer, a realm-wide read, the
database) fails the pass as a whole, and so does a reconcile that could act on nobody.

**The circuit breaker (E9.F9.5.S7, issue #105; a mass "disabled", E9.F9.5.S8, issue #135).** Some Keycloak failures
make it answer 404 about many people who still exist: a realm restored from an older backup, the issuer pointing at
the wrong Keycloak, a non-importing user-storage provider (LDAP) disabled for maintenance. An *importing* provider
switched off makes it report every one of its people ``enabled: false`` instead. So the reconcile reads everyone
before it acts on anyone, and counts the people newly found gone with no DELETE_ACCOUNT event and the people newly
found disabled together (one already cut off, with a grace period running or not, was counted when it was found). At
``keycloak_breaker_min`` or more, and more than ``keycloak_breaker_percent`` of the people it read, the breaker trips:
the pass is recorded with those people and their kind (``sync_breaker_trips``, ``sync_breaker_people``),
``inkcast_identity_sync_breaker_open`` is 1 and ``InkcastIdentitySyncBreakerOpen`` fires. The gone are held as
before: nobody is cut off or erased and no grace period starts or advances. The disabled are **held** (``hold_person``,
``users.held_at``): their OAuth tokens, codes and pending consents go at once, which a sign-in restores and Keycloak
refuses while they are disabled, so it is reversible; their personal access tokens are refused but not revoked. It
stays open, and every later pass holds every 404 without an event and every ``enabled: false`` (the events pass too),
until an operator resolves the pass with ``inkcast identity confirm-deletions --pass <id>`` (the gone are cut off and
their grace period starts then, and the reconcile erases them as the policy says; the disabled are cut off and their
personal access tokens revoked) or ``dismiss-deletions`` (nothing is done to the gone; the disabled are released and
their personal access tokens work again; the next reconcile reads everyone again, and trips again if it must). A
held person Keycloak reports enabled again is released at once, by whichever pass reads them, and leaves the pass. A
DELETE_ACCOUNT event is never held: the person asked, and the event is specific. Below the thresholds a 404 or an
``enabled: false`` is acted on as before.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx2

from ...libs.db.identity import BREAKER_GONE

log = logging.getLogger("inkcast.identity.sync")
CURSOR = "keycloak-admin-events"
USER_EVENTS_CURSOR = "keycloak-user-events"
# Events become visible out of time order (stamped when a request starts, stored when it commits; replicas have
# their own clocks), so each pass also re-reads the minute before each cursor (admin and user events alike).
# Re-checking a person is harmless, and re-seeing an erased one is a no-op.
LOOKBACK_MS = 60_000
# A read about one person with no answer that can be that person's (#121, #152): the request reached Keycloak and
# nothing came back, which is what a user-storage lookup that hangs for that one person looks like from here (the
# client's read timeout; the connection closed, or cut by a proxy that gave up, without an answer). Every other
# transport error happens on the way there (a connection refused or reset, a connect timeout, the proxy, the
# client's own pool) and is never about one person: it fails the pass as a whole, at once.
PERSON_NO_ANSWER = (httpx2.ReadTimeout, httpx2.ReadError, httpx2.RemoteProtocolError)
# How many reads about people may get no answer in one pass before it ends (#152). Each costs the client's read
# timeout (30 s, libs/runtime), so a pass waits at most 90 s on them: two people who hang never block anyone, and
# an outage that begins mid-pass costs the pass three timeouts, not one per person. A constant, not a setting: it
# guards the pass's own duration, against the client's timeout and the sync interval, and is no policy an operator
# should tune. From three people who hang on, the passes that read only them (an events pass with their events and
# nobody else's) fail, and the pass that reconciles reads everyone else before them, so the rest are still cut off
# within one reconcile interval; such a realm needs those people fixed (InkcastIdentitySyncPersonFailing names
# them), not a longer pass. A smaller budget would let one or two hanging people end every pass, which is what #152
# removes.
TIMEOUT_BUDGET = 3


class SyncError(Exception):
    """The pass fails: Keycloak refused the service account or a realm-wide read, or the reconcile could act on nobody."""


class PersonError(SyncError):
    """Keycloak answered an error about one person (``users/{id}``, or their events), or gave that read no answer at
    all (#121): that person's alone, and the pass goes on (#103). ``call`` names the read (``user``, ``events``),
    ``status`` the HTTP status (``0`` for no answer) and ``cause`` the transport error's class name (``ReadTimeout``,
    ``ReadError``; empty when Keycloak answered): fixed strings and an int, all a log line about them carries."""

    def __init__(self, message: str, call: str, status: int, cause: str = ""):
        super().__init__(message)
        self.call, self.status, self.cause = call, status, cause


@dataclasses.dataclass
class _Pass:
    """What one pass (``tick``) knows about its reads about people (#152): whether any answered, how many got no
    answer (at ``TIMEOUT_BUDGET`` the pass ends) and whose, so nobody is read twice in one pass. It lives in the
    pass's own task (``_PASS``): a read outside a pass, the return page's in the request's task, has none of this and
    is one read that answers or not."""

    answered: bool = False
    timeouts: int = 0
    hung: set[str] = dataclasses.field(default_factory=set)

    @property
    def ended(self) -> bool:
        return self.timeouts >= TIMEOUT_BUDGET


_PASS: contextvars.ContextVar[_Pass | None] = contextvars.ContextVar("inkcast.identity.sync.pass", default=None)


def _user_not_found(r: httpx2.Response) -> bool:
    try:
        body = r.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "User not found"


def erase_person(store: Any, metrics: Any, user: dict[str, Any], via: str) -> dict[str, int]:
    """Erase one person whose account is gone (``via`` ``return`` for the visit back from Keycloak, ``event`` for
    the DELETE_ACCOUNT event pass, ``reconcile`` for the reconcile: a missed event, or the grace period over); logs
    ``identity.user.deleted`` with ids and counts only. ``user`` is their row as the caller read it before it decided
    (before it read Keycloak's 404, or when the grace period was found over): ``Store.erase_user`` re-checks it inside
    its transaction (#102), so a sign-in handled by another replica since (Keycloak let them in: it has them), or a
    pass elsewhere with newer word about them, erases nothing, and the next pass judges them afresh. Nothing to erase
    is not an error (another path got there first, or they are not as read), and is neither logged nor counted."""
    user_id = str(user["id"])
    counts = store.erase_user(user_id, as_read=user)
    if not any(counts.values()):
        return counts
    posts, tokens, pats = (int(counts.get(k, 0)) for k in ("posts", "tokens", "api_tokens"))
    log.warning("Inkcast user %s's account is gone (%s): erased %d posts, %d OAuth grants, %d PATs, %d rows in all",
                user_id, via, posts, tokens, pats, sum(counts.values()),
                extra={"event": "identity.user.deleted", "user_id": user_id, "via": via, "rows": sum(counts.values())})
    if metrics:
        metrics.inc("inkcast_identity_users_deleted_total", via=via)
    return counts


def cut_off_person(store: Any, metrics: Any, user: dict[str, Any], state: str, via: str, now: float) -> bool | None:
    """Cut one person off once (tokens, codes, PATs; ``disabled_at``). A person Keycloak no longer has (state
    ``deleted``) is also marked gone from ``now``, already cut off or not: the erasure grace period runs from the
    first time Inkcast acts on them being gone, on either path (``via`` ``event``, ``reconcile``, or ``confirm`` for
    an operator confirming a pass the breaker held). Logs ``identity.user.disabled`` with the Inkcast id, a fixed
    reason and counts only. ``user`` is their row as the caller read it: each step is taken only while its
    ``last_login_at`` is still their last sign-in (#102), so a sign-in handled by another replica in between (Keycloak
    let them in) leaves them as they are. True when they were cut off now; False when they were already (for
    ``deleted``, marked gone all the same); None when nothing was done because they are not as read."""
    user_id, last_login_at = str(user["id"]), float(user["last_login_at"])
    if state == "deleted" and not user.get("deleted_at"):
        if store.mark_user_deleted(user_id, now, last_login_at=last_login_at) is None:
            return None
    if user.get("disabled_at"):
        return False
    cut = store.disable_user(user_id, last_login_at=last_login_at, held_at=user.get("held_at"))   # a release since: enabled
    if cut is None:
        return None
    # Only the Inkcast id, a fixed reason and counts reach the log: nothing from Keycloak's answers or the
    # token tables (the identity key already ties the user id to the Keycloak id).
    reason = "deleted" if state == "deleted" else "disabled"
    oauth, pats, codes = (int(cut.get(k, 0)) for k in ("oauth_tokens", "personal_access_tokens", "codes"))
    log.warning("Keycloak %s the person behind Inkcast user %s: cut off %d OAuth grants, %d PATs, %d codes",
                reason, user_id, oauth, pats, codes,
                extra={"event": "identity.user.disabled", "user_id": user_id, "reason": reason, "via": via,
                       "revoked_oauth": oauth, "revoked_pats": pats, "revoked_codes": codes})
    if metrics:
        metrics.inc("inkcast_identity_users_disabled_total", reason=reason)
    return True


def hold_person(store: Any, user: dict[str, Any], via: str, now: float) -> bool:
    """Hold one person Keycloak reports disabled while the breaker holds a pass (#135): their OAuth tokens, codes and
    pending consents go now (a sign-in restores them, and Keycloak refuses that while they are disabled), their
    personal access tokens are refused but not revoked, until an operator confirms (revoked) or dismisses (work
    again). Nothing for one already held or cut off. Logs ``identity.user.held`` with the Inkcast id and counts."""
    user_id = str(user["id"])
    cut = store.hold_user(user_id, now)
    if cut is None:
        return False
    oauth, codes = (int(cut.get(k, 0)) for k in ("oauth_tokens", "codes"))     # counts only, as in cut_off_person
    log.warning("Keycloak disabled the person behind Inkcast user %s while the sync's breaker holds a pass: held; cut off "
                "%d OAuth grants and %d codes, their personal access tokens refused until an operator decides",
                user_id, oauth, codes,
                extra={"event": "identity.user.held", "user_id": user_id, "via": via, "revoked_oauth": oauth, "revoked_codes": codes})
    return True


def release_person(store: Any, user_id: str, via: str) -> bool:
    """Lift the hold on one person (``via`` ``reconcile`` or ``event``: Keycloak reports them enabled again; ``dismiss``:
    an operator dismissed the pass): their personal access tokens work again. Logs ``identity.user.released``."""
    if not store.release_user(user_id):
        return False
    log.info("The hold on Inkcast user %s is lifted (%s): their personal access tokens work again", user_id, via,
             extra={"event": "identity.user.released", "user_id": user_id, "via": via})
    return True


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
        # Keycloak id -> when their last read got no answer (#152): the reconcile reads them last, the longest ago
        # first, so people who hang delay each other and nobody else. A read that answers takes them out.
        self._hanging: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.keycloak_sync_client_id)

    # ---- the admin API --------------------------------------------------------------------------------------

    async def _access_token(self) -> str:
        if self._token and self.clock() < self._token_until:
            return self._token
        try:
            r = await self.http.post(self.token_url, data={
                "grant_type": "client_credentials", "client_id": self.config.keycloak_sync_client_id,
                "client_secret": self.config.keycloak_sync_secret.reveal()}, headers={"Accept": "application/json"})
        except httpx2.TransportError as exc:    # the service account's failure, never a person's (#121)
            raise SyncError(f"no answer from Keycloak's token endpoint ({type(exc).__name__})") from exc
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

    async def _read_person(self, keycloak_id: str, path: str, params: Any, call: str) -> httpx2.Response:
        """``_get`` for a read about one person. No answer once the request reached Keycloak (``PERSON_NO_ANSWER``: a
        timeout, the connection closed without an answer) is that person's alone, like an error Keycloak answers
        (``PersonError``, status 0, the error's name), so one person whose read hangs blocks nobody after them (#121);
        inside a pass it is one of the pass's ``TIMEOUT_BUDGET`` (#152), and the pass reads them no more. A transport
        error on the way there (a connection refused or reset, the client's pool) is never one person's: it fails the
        pass (``SyncError``). Either way only the error's class name is kept: nothing of its message. An answer,
        whatever it says, is Keycloak answering about a person. Whose last read got no answer is remembered
        (``_hanging``), for the reconcile's order."""
        p = _PASS.get()
        try:
            r = await self._get(path, params)
        except httpx2.TransportError as exc:
            cause = type(exc).__name__
            if not isinstance(exc, PERSON_NO_ANSWER):
                raise SyncError(f"no answer from Keycloak to the {call} read ({cause}): on the way there, not about a "
                                "person") from exc
            self._hanging[keycloak_id] = self.clock()
            if p is not None:
                p.timeouts += 1
                p.hung.add(keycloak_id)
            raise PersonError(f"the {call} read got no answer ({cause})", call, 0, cause) from exc
        self._hanging.pop(keycloak_id, None)
        if p is not None:
            p.answered = True
        return r

    async def person(self, keycloak_id: str) -> str:
        """'active', 'disabled' or 'deleted', as Keycloak sees them now. Only Keycloak's own answer for a missing
        user (404 ``{"error": "User not found"}``, UsersResource) means 'deleted': a 404 from anything else (a proxy
        that doesn't expose /admin/, a wrong URL) is an error, never a reason to erase or cut anyone off."""
        r = await self._read_person(keycloak_id, f"users/{quote(keycloak_id, safe='')}", None, "user")   # one path segment: '#', '?' or '/' ask about another id
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
        r = await self._read_person(keycloak_id, "events", [("type", "DELETE_ACCOUNT"), ("user", keycloak_id),
                                                            ("first", "0"), ("max", "1")], "events")
        if r.status_code != 200:
            raise PersonError(f"reading events answered HTTP {r.status_code}", "events", r.status_code)
        return any(e.get("type") == "DELETE_ACCOUNT" and e.get("userId") == keycloak_id for e in r.json())

    # ---- what Inkcast does about it ------------------------------------------------------------------------

    def _erase(self, user: dict[str, Any], via: str) -> bool:
        """``erase_person`` for ``user``, their row as this pass read it before it read Keycloak about them (#102);
        whether rows were erased now."""
        return any(erase_person(self.store, self.metrics, user, via).values())

    def _cut_off(self, user: dict[str, Any], state: str, via: str) -> bool | None:
        """``cut_off_person`` for ``user``, their row as this pass read it before it read Keycloak about them (#102):
        a row read after the answer would already hold a sign-in that overtook it."""
        return cut_off_person(self.store, self.metrics, user, state, via, self.clock())

    def _grace_over(self, listed: dict[str, Any]) -> dict[str, Any] | None:
        """The row, as it stands now, of a person Keycloak no longer has (``deleted_at`` set by ``_cut_off``) who has
        been gone for ``keycloak_erase_after_days``: exactly that many days counts, ``0`` is the first reconcile that
        finds them gone, ``None`` (``off``) is never. None when they signed in since ``listed`` was read: Keycloak let
        them in after the 404 this reconcile is acting on. The row is what the erasure re-checks (#102)."""
        days = self.config.keycloak_erase_after_days
        user = self.store.get_user(str(listed["id"])) if days is not None else None
        if not user or user.get("deleted_at") is None or user.get("last_login_at") != listed.get("last_login_at"):
            return None
        return user if self.clock() - float(user["deleted_at"]) >= days * 86400 else None

    def _person_failed(self, user_id: str | None, via: str, exc: PersonError) -> None:
        """Keycloak answered about one person, but not something the sync can act on, or gave the read no answer at
        all (#121): counted and logged, and the pass goes on with the others (#103); the person is read again at the
        next pass or reconcile. Only the Inkcast user id (None for a person Inkcast does not know), which read it
        was, the HTTP status and the transport error's name reach the log: never the Keycloak id, nor anything from
        the answer or the error."""
        extra = {"event": "identity.sync.person.failed", "user_id": user_id, "via": via, "call": exc.call,
                 "status": exc.status, "cause": exc.cause}
        if exc.status:
            log.warning("Keycloak answered HTTP %d to the %s read about Inkcast user %s (%s): retried at the next read; "
                        "the others go on", exc.status, exc.call, user_id, via, extra=extra)
        else:
            log.warning("Keycloak gave no answer (%s) to the %s read about Inkcast user %s (%s): not read again this "
                        "pass, retried at the next; the others go on", exc.cause, exc.call, user_id, via, extra=extra)
        if self.metrics:
            self.metrics.inc("inkcast_identity_sync_errors_total", stage="person")

    def _budget_used(self, p: _Pass, stage: str) -> bool:
        """Whether the pass ends here, its ``TIMEOUT_BUDGET`` of reads about people with no answer used (#152). With
        no read about a person answered since it began, the sync cannot tell three people from an outage and must
        not call it a success: ``SyncError``, and the pass fails as a whole (its cursors unmoved, the reconcile not
        done; the person lines name them). Otherwise it is a success for what was done, logged once with the stage it
        ended in and the count: fixed strings and ints."""
        if not p.ended:
            return False
        if not p.answered:
            raise SyncError(f"no answer from Keycloak to {p.timeouts} reads about people, and no read about a person "
                            f"answered since the pass began (the {stage} pass): an outage, or only people who hang")
        log.warning("The Keycloak user sync pass ends early: %d reads about people got no answer (the budget), in the %s "
                    "pass; what it did not reach waits for the next pass or reconcile", p.timeouts, stage,
                    extra={"event": "identity.sync.pass.ended", "stage": stage, "timeouts": p.timeouts})
        return True

    async def tick(self) -> dict[str, int]:
        """One pass: the events since the cursors, then a reconcile when it is due. Returns what it did. A
        ``PersonError`` is that person's alone (``_person_failed``); any other error, or a reconcile that could act
        on nobody (a realm-wide 403 or 500), fails the pass as a whole, as before. Reads about people with no answer
        are counted per pass (``_Pass``, in this task's context, so a read outside the pass counts for no pass): at
        ``TIMEOUT_BUDGET`` the pass ends, as a success for what it did unless none of them answered
        (``_budget_used``), and the next pass starts at nought."""
        done = {"checked": 0, "disabled": 0, "erased": 0}
        p = _Pass()
        token = _PASS.set(p)
        try:
            await self._pass(done, p)
        finally:
            _PASS.reset(token)
        self.last_success = self.clock()
        return done

    async def _pass(self, done: dict[str, int], p: _Pass) -> None:
        # When the reconcile runs in this pass it reads everyone, the people whose last read got no answer last
        # (#152), so the event passes leave those people to it: three of them with an event each, or edited within
        # the same minute, would otherwise use the budget at every pass before the reconcile, and block everyone else.
        due = self.clock() - self.last_reconcile >= self.config.keycloak_reconcile_seconds
        reconcile = due and self.clock() >= self.unread_until
        deleted, cursor, newest = await self.deleted_accounts()
        for keycloak_id, at in deleted.items():
            if p.ended or (reconcile and keycloak_id in self._hanging):   # not acted on: the cursor stops before their event
                newest = min(newest, at)
                continue
            # The look-back reads an event again on later passes, and Keycloak can have the person again under the
            # same id (restored from a realm export, LDAP without import): erase only once it confirms they are gone,
            # and only while they stand as read here, before that answer (#102).
            user = self.store.find_user_by_identity(self.prefix + keycloak_id)
            if not user:
                continue
            try:
                if await self.person(keycloak_id) == "deleted" and self._erase(user, "event"):
                    done["erased"] += 1
            except PersonError as exc:
                self._person_failed(str(user["id"]), "event", exc)
                newest = min(newest, at)         # the cursor stops before their event: read again until erased
        ended = self._budget_used(p, "events")     # raises when nothing answered: the cursor stays, as for any failure
        self.store.set_sync_state(USER_EVENTS_CURSOR, str(max(cursor, newest)))
        if ended:
            return
        held = self._breaker_open()                # no 404 without an event, and no enabled: false, is acted on until an operator decides
        for keycloak_id in await self.changed_people():
            if p.ended:                            # the rest are left to the reconcile, as a burst beyond one page is
                break
            if keycloak_id in p.hung or (reconcile and keycloak_id in self._hanging):   # the reconcile reads them, last (#152)
                continue
            done["checked"] += 1
            known = self.store.find_user_by_identity(self.prefix + keycloak_id)     # as they stand before the answer (#102)
            try:
                state = await self.person(keycloak_id)
            except PersonError as exc:                 # the reconcile re-reads everyone: like an event the pass missed
                self._person_failed(str(known["id"]) if known else None, "event", exc)
                continue
            if not known:
                continue
            if state == "deleted":                     # held: the reconcile reads them again, and acts once an operator decides
                if not held and self._cut_off(known, state, "event"):
                    done["disabled"] += 1
                continue
            self.store.mark_user_present(str(known["id"]))     # Keycloak has them: a running grace period stops
            if state == "active":                      # enabled (again): a hold on them is lifted by this pass (#135)
                if held:                               # and they leave the pass, as at the reconcile
                    self.store.release_breaker_people([str(known["id"])])
                if known.get("held_at") is not None:
                    release_person(self.store, str(known["id"]), "event")
            elif held:                                 # disabled while a pass is held: held too, not cut off (#135)
                hold_person(self.store, known, "event", self.clock())
            elif cut_off_person(self.store, self.metrics, known, state, "event", self.clock()):
                done["disabled"] += 1
        if self._budget_used(p, "admin-events"):   # the reconcile is not done: due again at the next pass
            return
        if due:
            if not reconcile:                       # still failing, but everyone is read again on schedule
                raise SyncError("the last reconcile could act on nobody; everyone is read again at the next one")
            await self._reconcile(done, p)
            self.last_reconcile = self.clock()
            self._budget_used(p, "reconcile")       # ran, on whoever it read: the next is on schedule

    async def _reconcile(self, done: dict[str, int], p: _Pass) -> None:
        """Everyone Inkcast knows from this Keycloak, the cut-off people too: one Keycloak no longer has is erased once
        the grace period is over; one it has again (restored from a backup under the same id) gets a whole grace
        period at a later deletion. Everyone is read before anyone is acted on, so the breaker can count (#105) and
        so an error that fails the pass as a whole leaves nobody half done. The people whose last read got no answer
        are read last, the longest ago first (#152): once the pass's budget of such reads is used, nobody else is
        read, and whoever was read is acted on, but for what the breaker would hold while the people left untried,
        not known to hang, could take its count to the minimum."""
        people = self.store.users_with_identity_prefix(self.prefix, include_disabled=True)
        ids = [user["identity"][len(self.prefix):] for user in people]
        self._hanging = {k: at for k, at in self._hanging.items() if k in set(ids)}        # the erased leave it
        hanging = set(self._hanging)                 # known to hang before this reconcile's reads
        order = sorted(range(len(people)), key=lambda i: (ids[i] in self._hanging, self._hanging.get(ids[i], 0.0)))
        read: list[tuple[dict[str, Any], str, str]] = []           # (row as listed, Keycloak id, state)
        untried = 0                                  # left by the budget, not known to hang: the breaker can't count them
        for i in order:
            user, keycloak_id = people[i], ids[i]
            if p.ended or keycloak_id in p.hung:     # the budget is used, or no answer about them this pass already:
                untried += keycloak_id not in p.hung and keycloak_id not in hanging
                continue                             # read at the next reconcile, on schedule
            done["checked"] += 1
            try:
                state = await self.person(keycloak_id)
            except PersonError as exc:               # read again at the next reconcile, on schedule
                self._person_failed(str(user["id"]), "reconcile", exc)
                continue
            read.append((user, keycloak_id, state))
        if people and not read:                      # nobody could be read: the sync is failing, not a person
            self.unread_until = self.clock() + self.config.keycloak_reconcile_seconds
            raise SyncError(f"the reconcile could act on nobody: none of {len(people)} people could be read")
        gone: list[tuple[dict[str, Any], str, bool]] = []          # (row, Keycloak id, deleted their own account)
        for user, keycloak_id, state in read:
            if state != "deleted":
                continue
            if p.ended:                              # no read after the budget: gone for certain, cut off now; the
                own_deletion = False                 # event, next reconcile, as when this read gets no answer
            else:
                try:
                    own_deletion = await self.deleted_own_account(keycloak_id)
                except PersonError as exc:           # gone for certain: cut off now; the event, next reconcile
                    self._person_failed(str(user["id"]), "reconcile", exc)
                    own_deletion = False
            gone.append((user, keycloak_id, own_deletion))
        disabled = [(user, keycloak_id) for user, keycloak_id, state in read if state == "disabled"]
        held = self._breaker(gone, disabled, len(read), untried)
        deferred = held is None                      # the breaker could not count: what it would hold waits (#152)
        if held:                                     # Keycloak has them again: a confirmation no longer applies to them
            self.store.release_breaker_people([str(user["id"]) for user, _, state in read if state == "active"])
            self.store.release_breaker_people([str(user["id"]) for user, _ in disabled], BREAKER_GONE)   # present, still disabled
        for user, keycloak_id, state in read:
            if state == "deleted":
                continue
            if user.get("deleted_at") is not None:   # present, enabled or not: never erased
                self.store.mark_user_present(str(user["id"]))
            if state == "active":                    # enabled (again): a hold on them is lifted (#135)
                if user.get("held_at") is not None:
                    release_person(self.store, str(user["id"]), "reconcile")
            elif held:                               # disabled, and the breaker holds a pass: held, not cut off (#135)
                self._hold(user, "reconcile")
            elif deferred:                           # judged by a reconcile that reads everyone it can
                continue
            elif self._cut_off(user, state, "reconcile"):    # as listed, before the reads (#102)
                done["disabled"] += 1
        for user, keycloak_id, own_deletion in gone:
            if own_deletion:                         # an event the pass missed: erased, as the pass would have
                if self._erase(user, "reconcile"):   # as listed: nothing in this pass has touched them since
                    done["erased"] += 1
            elif held or deferred or self._signed_in_since(user):
                continue                             # held for an operator, or Keycloak let them in after the 404
            else:                                    # an administrator's deletion, or the event is past retention
                if self._cut_off(user, "deleted", "reconcile"):    # as listed: a sign-in since the check declines it
                    done["disabled"] += 1
                over = self._grace_over(user)        # their row now, cut off; what the erasure re-checks
                if over is not None and self._erase(over, "reconcile"):
                    done["erased"] += 1

    def _signed_in_since(self, listed: dict[str, Any]) -> bool:
        """Whether the person signed in (or was erased) since ``listed`` was read: everyone is read before anyone is
        acted on, and a sign-in through Keycloak in between is the one proof Inkcast has that Keycloak has them."""
        user = self.store.get_user(str(listed["id"]))
        return not user or user.get("last_login_at") != listed.get("last_login_at")

    def _hold(self, listed: dict[str, Any], via: str) -> bool:
        """``hold_person`` for a person the reconcile read disabled, unless they signed in since ``listed`` was read
        (Keycloak let them in: enabled, the one proof) or were erased; nothing for one already held or cut off."""
        if self._signed_in_since(listed):
            return False
        return hold_person(self.store, listed, via, self.clock())

    def _breaker_open(self) -> bool:
        """Whether a pass is held for an operator, with the breaker on; with ``INKCAST_KEYCLOAK_BREAKER=off`` nothing
        is held, whatever a pass recorded earlier says (it stays listed until resolved)."""
        return bool(self.config.keycloak_breaker and self.store.pending_breaker_trip())

    def _breaker(self, gone: list[tuple[dict[str, Any], str, bool]], disabled: list[tuple[dict[str, Any], str]],
                 read: int, untried: int = 0) -> bool | None:
        """The circuit breaker (#105, #135): whether this reconcile holds every 404 without a DELETE_ACCOUNT event
        and every ``enabled: false``. It counts the people newly found gone (no event, no ``deleted_at`` yet) and
        newly found disabled (no ``disabled_at`` yet) together against the people read (one already cut off was
        counted when it was found, and confirming a pass marks its people); at the minimum and above the share it
        records the pass, each person with their kind, and trips. An open breaker stays open, and records no second
        pass, until an operator resolves the first (``inkcast identity``). None when the pass's budget left
        ``untried`` people not known to hang who could take the count to the minimum (#152): it cannot count, so
        nothing it would hold is done until a reconcile reads them."""
        if not self.config.keycloak_breaker:
            return False
        pending = self.store.pending_breaker_trip()
        held = sum(1 for _, _, own_deletion in gone if not own_deletion)
        if pending is not None:
            log.warning("The Keycloak sync's breaker is open (pass %s): %d people Keycloak says are gone and %d it says are "
                        "disabled are held, neither cut off nor erased, until an operator confirms or dismisses the pass",
                        pending["id"], held, len(disabled),
                        extra={"event": "identity.sync.breaker.held", "pass_id": pending["id"], "held": held,
                               "disabled": len(disabled)})
            return True
        # As their row stands now, not as listed before the reads: a pass confirmed meanwhile has marked its people.
        newly_gone = [user for user, _, own_deletion in gone if not own_deletion and user.get("deleted_at") is None
                      and (row := self.store.get_user(str(user["id"]))) is not None and row.get("deleted_at") is None]
        newly_disabled = [user for user, _ in disabled if user.get("disabled_at") is None
                          and (row := self.store.get_user(str(user["id"]))) is not None and row.get("disabled_at") is None]
        newly, cfg = len(newly_gone) + len(newly_disabled), self.config
        if untried and newly + untried >= cfg.keycloak_breaker_min:
            return None
        if not (newly >= cfg.keycloak_breaker_min and newly * 100 > cfg.keycloak_breaker_percent * read):
            return False
        pass_id = self.store.record_breaker_trip(self.clock(), len(newly_gone), read,
                                                 [(str(user["id"]), float(user["last_login_at"])) for user in newly_gone],
                                                 [(str(user["id"]), float(user["last_login_at"])) for user in newly_disabled])
        log.critical("Keycloak says %d of the %d people read are gone and %d disabled at once (at least %d and more than "
                     "%d%% in all): nobody is cut off or erased and no grace period starts, and the disabled keep their "
                     "personal access tokens, refused, until an operator confirms or dismisses pass %s "
                     "(inkcast identity confirm-deletions | dismiss-deletions --pass %s)", len(newly_gone), read,
                     len(newly_disabled), cfg.keycloak_breaker_min, cfg.keycloak_breaker_percent, pass_id, pass_id,
                     extra={"event": "identity.sync.breaker.tripped", "pass_id": pass_id, "gone": len(newly_gone),
                            "disabled": len(newly_disabled), "read": read, "held": held})
        return True

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

"""Fake upstreams for tests: a Keycloak-style OIDC provider, Medium's API, the Forem (DEV) API and an OpenFGA
server (its REST API over the real model evaluator).

They follow the documented request/response shapes closely enough to catch
contract mistakes (field names, auth headers, PKCE, limits)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from inkcast.libs.authz import EngineError, Evaluator, InvalidRequest, Model, Tuple


class Fakes:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.issuer = f"{self.base}/kc/realms/inkcast"
        self.oidc_client = ("inkcast", "kc-secret")
        # Who the fake IdP signs in next (tests can change it).
        self.next_user = {"sub": "kc-user-1", "preferred_username": "ada", "email": "ada@example.com",
                          "email_verified": True, "name": "Ada Lovelace", "realm_access": {"roles": ["inkcast-user"]}}
        self._codes: dict[str, dict] = {}
        # Keycloak's own login form, when set: username -> (password, user claims). None signs `next_user` in at once.
        self.kc_accounts: dict[str, tuple[str, dict]] | None = None
        self._kc_sessions: dict[str, dict] = {}
        self._kc_tokens_by_key: dict[str, str] = {}
        self.kc_mailbox: list[dict] = []                # what Mailpit would hold; served at /mailpit/api/v1
        self._kc_tokens: dict[str, dict] = {}
        # Keycloak's admin API, as the inkcast-sync service account sees it (E9.F9.5.S2): sub -> {"enabled": bool};
        # a sub not listed is an enabled person, one in kc_deleted is gone (404). Admin events, newest last.
        self.kc_sync_client = ("inkcast-sync", "sync-secret")
        self.kc_people: dict[str, dict] = {}
        self.kc_deleted: set[str] = set()
        self.kc_admin_events: list[dict] = []
        self.kc_admin_calls: list[str] = []
        self._kc_sync_tokens: set[str] = set()
        self.medium_posts: list[tuple[str, dict]] = []
        self.devto_articles: list[dict] = []
        self.devto_fail_next: int | None = None  # e.g. 429 to simulate rate limiting
        self.devto_updates: list[tuple[int, dict]] = []  # (article id, article) for every PUT
        # OpenFGA (E3.F3.6.S2): one store, one model (the packaged one), a preshared key, tuples in memory. The
        # answers come from the same Evaluator the LocalEngine uses, so this fake checks the HTTP contract only.
        self.fga_store = "01JSTORE0000000000000000AA"
        self.fga_model = "01JMODEL0000000000000000AA"
        self.fga_token: str | None = "fga-preshared-key"
        self.fga_tuples: list[tuple[str, str, str]] = []
        self.fga_calls: list[tuple[str, str, dict | None]] = []
        self.fga_fail_next: int | None = None  # answer the next call with this status (503, 429...)
        self._fga_evaluator = Evaluator(Model.load(), _FgaSource(self))

    # ---- Keycloak-style OIDC ------------------------------------------------

    async def oidc_config(self, _):
        return JSONResponse({
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
            "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
            "userinfo_endpoint": f"{self.issuer}/protocol/openid-connect/userinfo",
        })

    async def oidc_auth(self, request: Request):
        q = request.query_params
        assert q["client_id"] == self.oidc_client[0] and q["code_challenge_method"] == "S256"
        assert "openid" in q["scope"].split()
        if self.kc_accounts is not None:                   # the page a person fills in, with Keycloak's markup
            session = secrets.token_hex(8)
            self._kc_sessions[session] = {"q": dict(q), "user": None, "pending": []}
            return self._kc_login_page(session)
        code = secrets.token_hex(8)
        self._codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "user": dict(self.next_user)}
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", status_code=302)

    def _kc_login_page(self, session: str, error: str = "") -> HTMLResponse:
        action = f"{self.issuer}/login-actions/authenticate?session_code={session}&amp;execution=e1&amp;tab_id=t1"
        register = f"{self.issuer}/login-actions/registration?session_code={session}&amp;tab_id=t1"
        return HTMLResponse(f'<html><body>{error}<form id="kc-form-login" onsubmit="return true;" action="{action}" '
                            f'method="post"><input name="username"><input name="password" type="password"></form>'
                            f'<div id="kc-registration"><a href="{register}">Register</a></div></body></html>')

    def _kc_next(self, session: str):
        """Keycloak's required actions in order, then back to the client with a code."""
        state = self._kc_sessions[session]
        if "VERIFY_EMAIL" in state["pending"]:
            return HTMLResponse("<html><title>Email verification</title><body>You need to verify your email address "
                                "to activate your account.</body></html>")
        if "TERMS_AND_CONDITIONS" in state["pending"]:
            action = f"{self.issuer}/login-actions/required-action?execution=TERMS_AND_CONDITIONS&amp;session_code={session}"
            return HTMLResponse(f'<html><body><div id="kc-terms-text">Terms</div><form class="form-actions" '
                                f'action="{action}" method="POST"><input name="accept" id="kc-accept" type="submit" '
                                f'value="Accept"><input name="cancel" type="submit" value="Decline"></form></body></html>')
        q = state["q"]
        self._kc_sessions.pop(session)
        code = secrets.token_hex(8)
        self._codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "user": dict(state["user"])}
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", status_code=302)

    async def kc_authenticate(self, request: Request):
        session = request.query_params.get("session_code", "")
        f = await request.form()
        account = (self.kc_accounts or {}).get(str(f.get("username", "")))
        if session not in self._kc_sessions or not account or account[0] != f.get("password"):
            return self._kc_login_page(session, '<span id="input-error">Invalid username or password.</span>')
        self._kc_sessions[session]["user"] = account[1]
        return self._kc_next(session)

    async def kc_registration(self, request: Request):
        session = request.query_params.get("session_code", "")
        if session not in self._kc_sessions:
            return HTMLResponse("expired", status_code=400)
        if request.method == "GET":
            action = f"{self.issuer}/login-actions/registration?session_code={session}&amp;execution=r1&amp;tab_id=t1"
            return HTMLResponse(f'<html><body><form id="kc-register-form" action="{action}" method="post">'
                                f'<input name="email"><input name="password"></form></body></html>')
        f = await request.form()
        email = str(f.get("email", ""))
        if f.get("password") != f.get("password-confirm") or email in (self.kc_accounts or {}):
            return HTMLResponse("<span>Invalid</span>", status_code=200)
        user = {"sub": "kc-" + secrets.token_hex(4), "email": email, "preferred_username": email, "email_verified": False,
                "name": f"{f.get('firstName')} {f.get('lastName')}", "realm_access": {"roles": ["inkcast-user"]}}
        self.kc_accounts[email] = (str(f.get("password")), user)            # default role, like the realm
        self._kc_sessions[session].update(user=user, pending=["VERIFY_EMAIL", "TERMS_AND_CONDITIONS"])
        key = secrets.token_hex(8)
        self._kc_tokens_by_key[key] = session
        self.kc_mailbox.append({"ID": key, "To": email, "Text": f"Verify your email: {self.issuer}/login-actions/"
                                f"action-token?key={key}&client_id=inkcast&tab_id=t1\n"})
        return self._kc_next(session)

    async def kc_action_token(self, request: Request):
        session = self._kc_tokens_by_key.pop(request.query_params.get("key", ""), None)
        if session not in self._kc_sessions:
            return HTMLResponse("This link has expired.", status_code=400)
        state = self._kc_sessions[session]
        state["user"]["email_verified"] = True
        state["pending"].remove("VERIFY_EMAIL")
        return self._kc_next(session)

    async def kc_required_action(self, request: Request):
        session = request.query_params.get("session_code", "")
        f = await request.form()
        if session not in self._kc_sessions or request.query_params.get("execution") != "TERMS_AND_CONDITIONS":
            return HTMLResponse("expired", status_code=400)
        if f.get("accept") != "Accept":
            return HTMLResponse("You need to accept the terms.", status_code=200)
        self._kc_sessions[session]["pending"].remove("TERMS_AND_CONDITIONS")
        return self._kc_next(session)

    async def mailpit_search(self, request: Request):
        to = re.search(r'to:"([^"]+)"', request.query_params.get("query", ""))
        found = [m for m in self.kc_mailbox if to and m["To"] == to.group(1)]
        return JSONResponse({"messages": [{"ID": m["ID"]} for m in reversed(found)]})

    async def mailpit_message(self, request: Request):
        m = next((m for m in self.kc_mailbox if m["ID"] == request.path_params["mid"]), None)
        return JSONResponse({"Text": m["Text"], "HTML": ""}) if m else JSONResponse({}, status_code=404)

    def kc_admin_change(self, sub: str, *, enabled: bool | None = None, delete: bool = False) -> None:
        """What an administrator does in Keycloak's console: the state changes and an admin event is recorded."""
        if delete:
            self.kc_deleted.add(sub)
        else:
            self.kc_people.setdefault(sub, {})["enabled"] = enabled
        self.kc_admin_events.append({"time": int(time.time() * 1000), "resourceType": "USER",
                                     "operationType": "DELETE" if delete else "UPDATE", "resourcePath": f"users/{sub}"})

    def _kc_admin_ok(self, request: Request) -> bool:
        return request.headers.get("authorization", "").removeprefix("Bearer ") in self._kc_sync_tokens

    async def kc_admin_user(self, request: Request):
        self.kc_admin_calls.append(request.url.path)
        if not self._kc_admin_ok(request):
            return JSONResponse({"error": "HTTP 401 Unauthorized"}, status_code=401)
        sub = request.path_params["uid"]
        if sub in self.kc_deleted:
            return JSONResponse({"error": "User not found"}, status_code=404)
        return JSONResponse({"id": sub, "enabled": self.kc_people.get(sub, {}).get("enabled", True)})

    async def kc_admin_events_list(self, request: Request):
        self.kc_admin_calls.append(request.url.path)
        if not self._kc_admin_ok(request):
            return JSONResponse({"error": "HTTP 401 Unauthorized"}, status_code=401)
        q = request.query_params
        kinds, ops = set(q.getlist("resourceTypes")), set(q.getlist("operationTypes"))
        found = [e for e in self.kc_admin_events
                 if (not kinds or e["resourceType"] in kinds) and (not ops or e["operationType"] in ops)]
        return JSONResponse(list(reversed(found))[: int(q.get("max", "100"))])   # Keycloak: newest first

    async def oidc_token(self, request: Request):
        f = await request.form()
        if f.get("grant_type") == "client_credentials":
            if (f.get("client_id"), f.get("client_secret")) != self.kc_sync_client:
                return JSONResponse({"error": "unauthorized_client"}, status_code=401)
            token = "sync-" + secrets.token_hex(8)
            self._kc_sync_tokens.add(token)
            return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_in": 300})
        grant = self._codes.pop(f.get("code", ""), None)
        if not grant or (f.get("client_id"), f.get("client_secret")) != self.oidc_client:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        digest = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).decode().rstrip("=")
        if digest != grant["challenge"] or f.get("redirect_uri") != grant["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        # Like Keycloak: realm roles travel in the (JWT) access token, not in userinfo.
        user = grant["user"]
        claims = {"sub": user["sub"], "realm_access": user.get("realm_access", {"roles": []}), "jti": secrets.token_hex(8)}
        b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
        token = f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"
        self._kc_tokens[token] = {k: v for k, v in user.items() if k != "realm_access"}
        return JSONResponse({"access_token": token, "token_type": "Bearer"})

    async def oidc_userinfo(self, request: Request):
        user = self._kc_tokens.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        return JSONResponse(user) if user else JSONResponse({"error": "invalid_token"}, status_code=401)

    # ---- Medium API v1 ------------------------------------------------------

    def _medium_ok(self, request: Request) -> bool:
        return request.headers.get("authorization") == "Bearer medium-token"

    async def medium_me(self, request: Request):
        if not self._medium_ok(request):
            return JSONResponse({"errors": [{"message": "Token was invalid.", "code": 6003}]}, status_code=401)
        return JSONResponse({"data": {"id": "m-ada", "username": "ada", "name": "Ada L", "url": "https://medium.com/@ada"}})

    async def medium_pubs(self, request: Request):
        if not self._medium_ok(request):
            return JSONResponse({}, status_code=401)
        return JSONResponse({"data": [
            {"id": "pub-bp", "name": "Better Programming", "url": "https://medium.com/better-programming"},
            {"id": "pub-other", "name": "Somebody Else's", "url": "https://medium.com/else"},
        ]})

    async def medium_contributors(self, request: Request):
        pub = request.path_params["pub"]
        data = [{"publicationId": pub, "userId": "m-ada", "role": "writer"}] if pub == "pub-bp" else []
        return JSONResponse({"data": data})

    async def medium_create(self, request: Request):
        if not self._medium_ok(request):
            return JSONResponse({}, status_code=401)
        body = await request.json()
        assert body["contentFormat"] in ("markdown", "html") and body["title"] and body["content"]
        where = request.url.path
        self.medium_posts.append((where, body))
        n = len(self.medium_posts)
        return JSONResponse({"data": {"id": f"mp{n}", "title": body["title"], "url": f"https://medium.com/@ada/post-{n}",
                                      "publishStatus": body.get("publishStatus", "public"), "tags": body.get("tags", [])}},
                            status_code=201)

    # ---- Forem / DEV API ----------------------------------------------------

    def _devto_ok(self, request: Request) -> bool:
        return request.headers.get("api-key") == "devto-key" and "forem.api-v1" in request.headers.get("accept", "")

    async def devto_me(self, request: Request):
        if not self._devto_ok(request):
            return JSONResponse({"error": "unauthorized", "status": 401}, status_code=401)
        return JSONResponse({"id": 42, "username": "ada", "name": "Ada Lovelace"})

    async def devto_org(self, request: Request):
        if request.path_params["org"] == "analytical":
            return JSONResponse({"id": 7, "username": "analytical", "name": "Analytical Engines"})
        return JSONResponse({"error": "not found", "status": 404}, status_code=404)

    async def devto_create(self, request: Request):
        if not self._devto_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self.devto_fail_next:
            status, self.devto_fail_next = self.devto_fail_next, None
            return JSONResponse({"error": "Rate limit reached"}, status_code=status)
        article = (await request.json())["article"]
        if len(article.get("tags", [])) > 4:
            return JSONResponse({"error": "Tag list exceed the maximum of 4 tags", "status": 422}, status_code=422)
        self.devto_articles.append(article)
        n = len(self.devto_articles)
        return JSONResponse({"id": 1000 + n, "url": f"https://dev.to/ada/article-{n}", "published": article["published"]},
                            status_code=201)

    async def devto_update(self, request: Request):
        if not self._devto_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        n = int(request.path_params["aid"]) - 1000
        if not 1 <= n <= len(self.devto_articles):
            return JSONResponse({"error": "not found", "status": 404}, status_code=404)
        article = (await request.json())["article"]
        self.devto_articles[n - 1] = article
        self.devto_updates.append((1000 + n, article))
        return JSONResponse({"id": 1000 + n, "url": f"https://dev.to/ada/article-{n}", "published": article["published"]})

    async def devto_mine(self, request: Request):
        if not self._devto_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([{"id": 1000 + i, "title": a["title"], "canonical_url": a.get("canonical_url"),
                              "url": f"https://dev.to/ada/article-{i}", "published": a["published"]}
                             for i, a in enumerate(self.devto_articles, 1)])

    # ---- OpenFGA REST API ----------------------------------------------------------------------------------
    # https://openfga.dev/api/service: field names, limits and error codes as documented; 400 bodies are
    # {"code": ..., "message": ...}. Every handler records (method, path, body) in fga_calls.

    _CONSISTENCY = ("UNSPECIFIED", "MINIMIZE_LATENCY", "HIGHER_CONSISTENCY")

    async def _fga_begin(self, request: Request):
        """Common checks: the preshared key, the store id, a forced failure; returns (body, None) or (None, response)."""
        body = await request.json() if request.method == "POST" else None
        self.fga_calls.append((request.method, request.url.path, body))
        if self.fga_fail_next:
            status, self.fga_fail_next = self.fga_fail_next, None
            return None, JSONResponse({"code": "internal_error", "message": "forced by the test"}, status_code=status)
        if self.fga_token and request.headers.get("authorization") != f"Bearer {self.fga_token}":
            return None, JSONResponse({"code": "unauthenticated", "message": "unauthenticated"}, status_code=401)
        if request.path_params["store"] != self.fga_store:
            return None, JSONResponse({"code": "store_id_not_found", "message": "store id not found"}, status_code=404)
        if body is not None and body.get("authorization_model_id", self.fga_model) != self.fga_model:
            return None, JSONResponse({"code": "authorization_model_not_found",
                                       "message": f"Authorization Model '{body['authorization_model_id']}' not found"},
                                      status_code=400)
        if body is not None and body.get("consistency", "UNSPECIFIED") not in self._CONSISTENCY:
            return None, self._fga_invalid(f"invalid consistency {body['consistency']!r}")
        return body, None

    @staticmethod
    def _fga_invalid(message: str, code: str = "validation_error") -> JSONResponse:
        return JSONResponse({"code": code, "message": message}, status_code=400)

    @staticmethod
    def _fga_key(key) -> tuple[str, str, str]:
        assert isinstance(key, dict) and set(key) <= {"user", "relation", "object", "condition"}, key
        for field in ("user", "relation", "object"):
            assert isinstance(key.get(field), str) and key[field], f"tuple_key.{field} is required"
        return key["user"], key["relation"], key["object"]

    def _fga_answer(self, user: str, relation: str, obj: str) -> bool:
        return self._fga_evaluator.check(user, relation, obj)

    async def fga_check(self, request: Request):
        body, refused = await self._fga_begin(request)
        if refused:
            return refused
        try:
            user, relation, obj = self._fga_key(body["tuple_key"])
            return JSONResponse({"allowed": self._fga_answer(user, relation, obj), "resolution": ""})
        except InvalidRequest as exc:
            return self._fga_invalid(str(exc))
        except EngineError as exc:
            return self._fga_invalid(str(exc), "authorization_model_resolution_too_complex")

    async def fga_batch_check(self, request: Request):
        body, refused = await self._fga_begin(request)
        if refused:
            return refused
        checks = body.get("checks")
        if not isinstance(checks, list) or not 1 <= len(checks) <= 50:
            return self._fga_invalid("batch check: 1 to 50 checks")
        result, seen = {}, set()
        for item in checks:
            cid = item.get("correlation_id", "")
            if not re.fullmatch(r"[\w\d-]{1,36}", cid) or cid in seen:
                return self._fga_invalid(f"invalid or duplicate correlation_id {cid!r}")
            seen.add(cid)
            try:
                result[cid] = {"allowed": self._fga_answer(*self._fga_key(item["tuple_key"]))}
            except EngineError as exc:
                result[cid] = {"error": {"input_error": "validation_error", "message": str(exc)}}
        return JSONResponse({"result": result})

    async def fga_list_objects(self, request: Request):
        body, refused = await self._fga_begin(request)
        if refused:
            return refused
        for field in ("type", "relation", "user"):
            if not isinstance(body.get(field), str) or not body[field]:
                return self._fga_invalid(f"{field} is required")
        try:
            return JSONResponse({"objects": self._fga_evaluator.list_objects(body["user"], body["relation"], body["type"])})
        except EngineError as exc:
            return self._fga_invalid(str(exc))

    async def fga_write(self, request: Request):
        body, refused = await self._fga_begin(request)
        if refused:
            return refused
        writes = [self._fga_key(k) for k in (body.get("writes") or {}).get("tuple_keys", [])]
        deletes = [self._fga_key(k) for k in (body.get("deletes") or {}).get("tuple_keys", [])]
        if not writes and not deletes:
            return self._fga_invalid("invalid WriteRequest: writes or deletes must be set")
        if len(writes) + len(deletes) > 100:
            return self._fga_invalid("at most 100 tuples per write")
        if len(set(writes + deletes)) != len(writes) + len(deletes):
            return self._fga_invalid("duplicate tuple in the write request")
        try:
            for t in writes:
                self._fga_evaluator.model.validate_tuple(Tuple(*t))
        except (InvalidRequest, ValueError) as exc:
            return self._fga_invalid(str(exc))
        for t in writes:
            if t in self.fga_tuples:
                return self._fga_invalid(f"cannot write a tuple which already exists: {t}", "write_failed_due_to_invalid_input")
        for t in deletes:
            if t not in self.fga_tuples:
                return self._fga_invalid(f"cannot delete a tuple which does not exist: {t}", "write_failed_due_to_invalid_input")
        self.fga_tuples = [t for t in self.fga_tuples if t not in deletes] + writes
        return JSONResponse({})

    async def fga_read(self, request: Request):
        body, refused = await self._fga_begin(request)
        if refused:
            return refused
        page = int(body.get("page_size", 50))
        if not 1 <= page <= 100:
            return self._fga_invalid("page_size must be between 1 and 100")
        rows = list(self.fga_tuples)
        key = body.get("tuple_key")
        if key is not None:
            obj = key.get("object", "")
            type_, _, obj_id = obj.partition(":")
            if not type_:
                return self._fga_invalid("tuple_key.object (a type or type:id) is required")
            if not obj_id and not key.get("user"):
                return self._fga_invalid("a type-only object needs a user")
            rows = [t for t in rows if (t[2] == obj if obj_id else t[2].startswith(type_ + ":"))]
            if key.get("user"):
                rows = [t for t in rows if t[0] == key["user"]]
            if key.get("relation"):
                rows = [t for t in rows if t[1] == key["relation"]]
        start = int(body.get("continuation_token") or 0)
        chunk = rows[start:start + page]
        token = str(start + page) if start + page < len(rows) else ""
        return JSONResponse({"tuples": [{"key": {"user": u, "relation": r, "object": o},
                                         "timestamp": "2026-01-01T00:00:00.000Z"} for u, r, o in chunk],
                             "continuation_token": token})

    async def fga_model_get(self, request: Request):
        _, refused = await self._fga_begin(request)
        if refused:
            return refused
        if request.path_params["model"] != self.fga_model:
            return self._fga_invalid("Authorization Model not found", "authorization_model_not_found")
        return JSONResponse({"authorization_model": {"id": self.fga_model, "schema_version": "1.1",
                                                     "type_definitions": [{"type": "user"}]}})

    def app(self) -> Starlette:
        kc = "/kc/realms/inkcast"
        return Starlette(routes=[
            Route(f"{kc}/.well-known/openid-configuration", self.oidc_config),
            Route(f"{kc}/protocol/openid-connect/auth", self.oidc_auth),
            Route(f"{kc}/login-actions/authenticate", self.kc_authenticate, methods=["POST"]),
            Route(f"{kc}/login-actions/registration", self.kc_registration, methods=["GET", "POST"]),
            Route(f"{kc}/login-actions/action-token", self.kc_action_token),
            Route(f"{kc}/login-actions/required-action", self.kc_required_action, methods=["POST"]),
            Route("/mailpit/api/v1/search", self.mailpit_search),
            Route("/mailpit/api/v1/message/{mid}", self.mailpit_message),
            Route(f"{kc}/protocol/openid-connect/token", self.oidc_token, methods=["POST"]),
            Route(f"{kc}/protocol/openid-connect/userinfo", self.oidc_userinfo),
            Route("/kc/admin/realms/inkcast/users/{uid}", self.kc_admin_user),
            Route("/kc/admin/realms/inkcast/admin-events", self.kc_admin_events_list),
            Route("/medium/v1/me", self.medium_me),
            Route("/medium/v1/users/{uid}/publications", self.medium_pubs),
            Route("/medium/v1/publications/{pub}/contributors", self.medium_contributors),
            Route("/medium/v1/users/{uid}/posts", self.medium_create, methods=["POST"]),
            Route("/medium/v1/publications/{pub}/posts", self.medium_create, methods=["POST"]),
            Route("/devto/api/users/me", self.devto_me),
            Route("/devto/api/organizations/{org}", self.devto_org),
            Route("/devto/api/articles", self.devto_create, methods=["POST"]),
            Route("/devto/api/articles/me/all", self.devto_mine),
            Route("/devto/api/articles/{aid:int}", self.devto_update, methods=["PUT"]),
            Route("/fga/stores/{store}/check", self.fga_check, methods=["POST"]),
            Route("/fga/stores/{store}/batch-check", self.fga_batch_check, methods=["POST"]),
            Route("/fga/stores/{store}/list-objects", self.fga_list_objects, methods=["POST"]),
            Route("/fga/stores/{store}/write", self.fga_write, methods=["POST"]),
            Route("/fga/stores/{store}/read", self.fga_read, methods=["POST"]),
            Route("/fga/stores/{store}/authorization-models/{model}", self.fga_model_get),
        ])


class _FgaSource:
    """The fake OpenFGA's tuples as the evaluator's ``TupleSource``."""

    def __init__(self, fakes: Fakes):
        self.fakes = fakes

    def users_of(self, obj: str, relation: str) -> list[str]:
        return [u for u, r, o in self.fakes.fga_tuples if o == obj and r == relation]

    def objects_of_type(self, type_: str) -> list[str]:
        return [o for _, _, o in self.fakes.fga_tuples if o.startswith(type_ + ":")]


class FakeRedis:
    """A Redis/Valkey server speaking RESP2 on a local port, in Python: GET, SET (PX/NX), DEL, INCRBY, PEXPIRE,
    PTTL, HSET, HMGET, PING, AUTH, SELECT, SCRIPT LOAD, EVAL, EVALSHA. It can't run Lua, so it carries a
    Python twin of each script Inkcast ships, chosen by the script's SHA-1 (the same hash EVALSHA uses).
    ``dropped`` counts connections the test cut (``kill()``), to test reconnects."""

    def __init__(self, password: str | None = None, clock=None):
        import time

        self.password = password
        self.clock = clock or time.time
        self.data: dict[str, object] = {}
        self.expires: dict[str, float] = {}
        self.scripts: dict[str, str] = {}
        self.commands: list[str] = []
        self.connections = 0
        self.server = None
        self._writers: list = []

    async def start(self) -> int:
        import asyncio
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.kill()
        self.server.close()
        await self.server.wait_closed()

    def kill(self) -> None:
        """Cut every open connection (a restart, a failover)."""
        for w in self._writers:
            w.close()
        self._writers.clear()

    # ---- the protocol -------------------------------------------------------------------

    async def _serve(self, reader, writer) -> None:
        from inkcast.libs.cache.resp import RespError, read_reply
        self.connections += 1
        self._writers.append(writer)
        authed = self.password is None
        try:
            while True:
                try:
                    args = await read_reply(reader)
                except (Exception, RespError):
                    return
                name = args[0].decode().upper()
                rest = [a.decode() if isinstance(a, bytes) else a for a in args[1:]]
                self.commands.append(name)
                if name == "AUTH":
                    authed = rest[-1] == self.password
                    reply = b"+OK\r\n" if authed else b"-WRONGPASS invalid username-password pair\r\n"
                elif not authed:
                    reply = b"-NOAUTH Authentication required.\r\n"
                else:
                    try:
                        reply = self._encode(self._dispatch(name, rest))
                    except RespError as exc:
                        reply = f"-{exc}\r\n".encode()
                writer.write(reply)
                await writer.drain()
        finally:
            writer.close()

    def _encode(self, value) -> bytes:
        if value is None:
            return b"$-1\r\n"
        if isinstance(value, bool):
            return b":%d\r\n" % int(value)
        if isinstance(value, int):
            return b":%d\r\n" % value
        if isinstance(value, str):
            if value.startswith("+"):
                return f"{value}\r\n".encode()
            data = value.encode()
            return b"$%d\r\n%s\r\n" % (len(data), data)
        if isinstance(value, list):
            return b"*%d\r\n" % len(value) + b"".join(self._encode(v) for v in value)
        raise TypeError(value)

    def _alive(self, key: str) -> bool:
        if key in self.expires and self.expires[key] <= self.clock():
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    def _dispatch(self, name: str, a: list):
        from inkcast.libs.cache.resp import RespError
        if name == "PING":
            return "+PONG"
        if name == "SELECT":
            return "+OK"
        if name == "GET":
            return str(self.data[a[0]]) if self._alive(a[0]) else None
        if name == "SET":
            key, value, opts = a[0], a[1], [str(o).upper() for o in a[2:]]
            if "NX" in opts and self._alive(key):
                return None
            self.data[key] = value
            self.expires.pop(key, None)
            if "PX" in opts:
                self.expires[key] = self.clock() + int(a[2 + opts.index("PX") + 1]) / 1000
            return "+OK"
        if name == "DEL":
            return int(self._alive(a[0]) and self.data.pop(a[0], None) is not None)
        if name == "INCRBY":
            value = (int(self.data[a[0]]) if self._alive(a[0]) else 0) + int(a[1])
            self.data[a[0]] = value
            return value
        if name == "PEXPIRE":
            if not self._alive(a[0]):
                return 0
            self.expires[a[0]] = self.clock() + int(a[1]) / 1000
            return 1
        if name == "PTTL":
            return int((self.expires[a[0]] - self.clock()) * 1000) if a[0] in self.expires and self._alive(a[0]) else -1
        if name == "HSET":
            h = self.data[a[0]] if self._alive(a[0]) else {}
            if not isinstance(h, dict):
                raise RespError("WRONGTYPE Operation against a key holding the wrong kind of value")
            for k, v in zip(a[1::2], a[2::2]):
                h[k] = v
            self.data[a[0]] = h
            return len(a[1::2])
        if name == "HMGET":
            h = self.data[a[0]] if self._alive(a[0]) else {}
            return [h.get(f) for f in a[1:]]
        if name == "SCRIPT":
            sha = hashlib.sha1(a[1].encode()).hexdigest()
            self.scripts[sha] = a[1]
            return sha
        if name in ("EVAL", "EVALSHA"):
            sha = hashlib.sha1(a[0].encode()).hexdigest() if name == "EVAL" else a[0]
            if name == "EVALSHA" and sha not in self.scripts:
                raise RespError("NOSCRIPT No matching script. Please use EVAL.")
            nkeys = int(a[1])
            keys, argv = a[2:2 + nkeys], a[2 + nkeys:]
            return self._script(sha, keys, argv)
        raise RespError(f"ERR unknown command '{name}'")

    def _script(self, sha: str, keys: list, argv: list):
        from inkcast.libs.cache.redis import TOKEN_BUCKET_SHA
        from inkcast.libs.cache.resp import RespError
        if sha != TOKEN_BUCKET_SHA:
            raise RespError("ERR the fake knows only Inkcast's token bucket script")
        capacity, rate, now, cost = (float(x) for x in argv)
        state = self._dispatch("HMGET", [keys[0], "t", "ts"])
        tokens, ts = (float(state[0]), float(state[1])) if state[0] is not None else (capacity, now)
        tokens = min(capacity, tokens + (now - ts) * rate)
        wait = 0.0
        if tokens >= cost:
            tokens -= cost
        else:
            wait = (cost - tokens) / rate
        self._dispatch("HSET", [keys[0], "t", repr(tokens), "ts", repr(now)])
        if rate > 0:
            self._dispatch("PEXPIRE", [keys[0], str(int(capacity / rate * 1000 + 0.999))])
        return repr(wait)

"""Fake upstreams for tests: a Keycloak-style OIDC provider, Medium's API and the Forem (DEV) API.

They follow the documented request/response shapes closely enough to catch
contract mistakes (field names, auth headers, PKCE, limits)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route


class Fakes:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.issuer = f"{self.base}/kc/realms/inkcast"
        self.oidc_client = ("inkcast", "kc-secret")
        # Who the fake IdP signs in next (tests can change it).
        self.next_user = {"sub": "kc-user-1", "preferred_username": "ada", "email": "ada@example.com",
                          "email_verified": True, "name": "Ada Lovelace", "realm_access": {"roles": ["inkcast-user"]}}
        self._codes: dict[str, dict] = {}
        self._kc_tokens: dict[str, dict] = {}
        self.medium_posts: list[tuple[str, dict]] = []
        self.devto_articles: list[dict] = []
        self.devto_fail_next: int | None = None  # e.g. 429 to simulate rate limiting
        self.devto_updates: list[tuple[int, dict]] = []  # (article id, article) for every PUT

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
        code = secrets.token_hex(8)
        self._codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "user": dict(self.next_user)}
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", status_code=302)

    async def oidc_token(self, request: Request):
        f = await request.form()
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

    def app(self) -> Starlette:
        kc = "/kc/realms/inkcast"
        return Starlette(routes=[
            Route(f"{kc}/.well-known/openid-configuration", self.oidc_config),
            Route(f"{kc}/protocol/openid-connect/auth", self.oidc_auth),
            Route(f"{kc}/protocol/openid-connect/token", self.oidc_token, methods=["POST"]),
            Route(f"{kc}/protocol/openid-connect/userinfo", self.oidc_userinfo),
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
        ])

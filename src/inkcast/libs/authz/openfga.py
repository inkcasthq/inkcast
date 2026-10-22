# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``OpenFgaEngine`` (E3.F3.6.S2): OpenFGA's REST API through ``httpx2``, with no SDK (ADR-0007).

Six calls, all under ``/stores/{store_id}``: ``check``, ``batch-check``, ``list-objects``, ``write``, ``read``
and ``GET authorization-models/{id}`` for readiness. Every request pins ``INKCAST_FGA_MODEL_ID`` (a model
change is a new version, never an edit in place) and carries the consistency the caller asked for.

Fail closed: a timeout, a connection error, a 5xx, a 401 or a 404 (wrong store or model id) is an
``EngineError``; a 400 (an unknown relation, a user type the model does not allow, a duplicate write) is an
``InvalidRequest``. Reads and checks are retried once when the connection could not be made (nothing was sent);
a write is never retried blindly, because a duplicate write is itself a 400 and the first attempt may have
landed. The preshared key (``INKCAST_FGA_API_TOKEN``) travels as a bearer header and nowhere else: not in logs,
not in error messages.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

import httpx2

from ..secrets import SecretValue
from .engine import MAX_BATCH_CHECK, Check
from .types import MINIMIZE_LATENCY, Consistency, EngineError, InvalidRequest, Ref, Tuple

CONSISTENCY = {"minimize_latency": "MINIMIZE_LATENCY", "higher_consistency": "HIGHER_CONSISTENCY"}
MAX_TUPLES_PER_WRITE = 100   # OPENFGA_MAX_TUPLES_PER_WRITE
READ_PAGE = 100              # the largest page Read allows
READY_TTL = 30.0             # how long a readiness answer is reused, so /readyz never hammers OpenFGA


class OpenFgaEngine:
    name = "openfga"

    def __init__(self, api_url: str, store_id: str, model_id: str, token: "SecretValue | str" = "",
                 http: httpx2.AsyncClient | None = None, timeout: float = 5.0, clock=time.monotonic):
        if not (api_url and store_id and model_id):
            raise EngineError("OpenFGA needs INKCAST_FGA_API_URL, INKCAST_FGA_STORE_ID and INKCAST_FGA_MODEL_ID")
        self.base = f"{api_url.rstrip('/')}/stores/{store_id}"
        self.store_id, self.model_id = store_id, model_id
        self.token = SecretValue(token)
        self.http = http or httpx2.AsyncClient()
        self.timeout = httpx2.Timeout(timeout, connect=min(timeout, 3.0))
        self.clock = clock
        self._ready: tuple[float, str | None] | None = None

    # ---- transport ------------------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token.reveal()}"
        return headers

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, retry: bool) -> dict[str, Any]:
        url = f"{self.base}/{path}"
        for attempt in (1, 2):
            try:
                r = await self.http.request(method, url, json=body, headers=self._headers(), timeout=self.timeout)
                break
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                if retry and attempt == 1:
                    continue  # the request never left: once more is safe
                raise EngineError(f"OpenFGA unreachable ({type(exc).__name__}) for {method} {path}") from exc
            except httpx2.HTTPError as exc:  # read timeouts, protocol errors: the request may have landed
                raise EngineError(f"OpenFGA request failed ({type(exc).__name__}) for {method} {path}") from exc
        if 200 <= r.status_code < 300:
            try:
                answer = r.json() if r.content else {}
            except ValueError as exc:
                raise EngineError(f"OpenFGA answered {method} {path} with a non-JSON body (HTTP {r.status_code})") from exc
            if not isinstance(answer, dict):
                raise EngineError(f"OpenFGA answered {method} {path} with a JSON {type(answer).__name__}, not an object")
            return answer
        code, message = _error(r)
        if r.status_code in (400, 422):
            raise InvalidRequest(f"OpenFGA refused {method} {path} ({code}): {message}")
        if r.status_code in (401, 403):
            raise EngineError(f"OpenFGA refused the credentials for {method} {path} (HTTP {r.status_code}, {code})")
        if r.status_code == 404:
            raise EngineError(f"OpenFGA store or model not found for {method} {path} (HTTP 404, {code})")
        raise EngineError(f"OpenFGA answered {method} {path} with HTTP {r.status_code} ({code})")

    # ---- the PolicyEngine -----------------------------------------------------------------------------------

    async def check(self, user: str, relation: str, obj: Ref, *, consistency: Consistency = MINIMIZE_LATENCY) -> bool:
        body = {"tuple_key": Tuple.of(user, relation, obj).as_key(), "authorization_model_id": self.model_id,
                "consistency": _consistency(consistency)}
        answer = await self._request("POST", "check", body, retry=True)
        return _flag(answer, "allowed", "check")

    async def batch_check(self, items: Sequence[Check], *, consistency: Consistency = MINIMIZE_LATENCY) -> list[bool]:
        if not items:
            return []
        out: list[bool] = []
        for start in range(0, len(items), MAX_BATCH_CHECK):
            chunk = items[start:start + MAX_BATCH_CHECK]
            body = {"checks": [{"tuple_key": Tuple.of(u, r, o).as_key(), "correlation_id": str(i)}
                               for i, (u, r, o) in enumerate(chunk)],
                    "authorization_model_id": self.model_id, "consistency": _consistency(consistency)}
            answer = await self._request("POST", "batch-check", body, retry=True)
            results = answer.get("result")
            if not isinstance(results, dict):
                raise EngineError("OpenFGA batch-check answered without results")
            for i in range(len(chunk)):
                one = results.get(str(i))
                if not isinstance(one, dict) or "allowed" not in one:
                    error = (one or {}).get("error") or {} if isinstance(one, dict) else {}
                    raise InvalidRequest(f"OpenFGA batch-check item {start + i} failed: "
                                         f"{error.get('input_error', 'no answer')}: {error.get('message', '')}")
                out.append(_flag(one, "allowed", "batch-check"))
        return out

    async def list_objects(self, user: str, relation: str, type_: str, *,
                           consistency: Consistency = MINIMIZE_LATENCY) -> list[str]:
        body = {"authorization_model_id": self.model_id, "type": type_, "relation": relation, "user": user,
                "consistency": _consistency(consistency)}
        answer = await self._request("POST", "list-objects", body, retry=True)
        objects = answer.get("objects")
        if not isinstance(objects, list):
            raise EngineError("OpenFGA list-objects answered without objects")
        return [str(o) for o in objects]

    async def write(self, writes: Iterable[Tuple] = (), deletes: Iterable[Tuple] = ()) -> None:
        writes, deletes = list(writes), list(deletes)
        if not writes and not deletes:
            raise InvalidRequest("a write needs at least one tuple to write or delete")
        if len(writes) + len(deletes) > MAX_TUPLES_PER_WRITE:
            raise InvalidRequest(f"a write takes at most {MAX_TUPLES_PER_WRITE} tuples (got {len(writes) + len(deletes)})")
        body: dict[str, Any] = {"authorization_model_id": self.model_id}
        if writes:
            body["writes"] = {"tuple_keys": [t.as_key() for t in writes]}
        if deletes:
            body["deletes"] = {"tuple_keys": [t.as_key() for t in deletes]}
        await self._request("POST", "write", body, retry=False)

    async def read(self, *, user: str | None = None, relation: str | None = None, obj: Ref | None = None,
                   type_: str | None = None) -> list[Tuple]:
        if obj is None and type_ is None and (user is not None or relation is not None):
            raise InvalidRequest("reading by user or relation needs an object or an object type")
        key: dict[str, str] = {}
        if obj is not None:
            key["object"] = str(obj)
        elif type_ is not None:
            key["object"] = f"{type_}:"
        if user is not None:
            key["user"] = user
        if relation is not None:
            key["relation"] = relation
        found: list[Tuple] = []
        token = ""
        while True:
            body: dict[str, Any] = {"page_size": READ_PAGE}
            if key:
                body["tuple_key"] = key
            if token:
                body["continuation_token"] = token
            answer = await self._request("POST", "read", body, retry=True)
            rows = answer.get("tuples") or []
            if not isinstance(rows, list):
                raise EngineError("OpenFGA read answered tuples that are not a list")
            for row in rows:
                try:
                    found.append(Tuple.from_key(row["key"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise EngineError(f"OpenFGA read answered a malformed tuple: {type(exc).__name__}") from exc
            token = str(answer.get("continuation_token") or "")
            if not token:
                return found

    async def ready(self) -> str | None:
        """The pinned model exists in the store. The answer (good or bad) is reused for ``READY_TTL`` seconds."""
        now = self.clock()
        if self._ready and now - self._ready[0] < READY_TTL:
            return self._ready[1]
        try:
            answer = await self._request("GET", f"authorization-models/{self.model_id}", retry=True)
            model = answer.get("authorization_model")
            found = str(model.get("id", "")) if isinstance(model, dict) else ""
            problem = None if found == self.model_id else f"OpenFGA answered with model {found!r}, not {self.model_id!r}"
        except EngineError as exc:
            problem = str(exc)
        self._ready = (now, problem)
        return problem


def _consistency(value: str) -> str:
    try:
        return CONSISTENCY[value]
    except KeyError:
        raise InvalidRequest(f"unknown consistency {value!r}: minimize_latency or higher_consistency") from None


def _flag(answer: dict[str, Any], field: str, call: str) -> bool:
    value = answer.get(field)
    if not isinstance(value, bool):
        raise EngineError(f"OpenFGA {call} answered without {field!r}")
    return value


def _error(r: httpx2.Response) -> tuple[str, str]:
    """OpenFGA's error body is ``{"code": ..., "message": ...}``; keep both short (never the whole body)."""
    try:
        body = r.json()
    except ValueError:
        return "no_json", ""
    if not isinstance(body, dict):
        return "no_json", ""
    return str(body.get("code", "unknown"))[:64], str(body.get("message", ""))[:300]

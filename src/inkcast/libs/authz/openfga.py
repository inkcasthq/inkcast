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

``OpenFgaAdmin`` (E3.F3.9.S1) is the operator's side of the same API, for ``inkcast authz write-model``: the store
by name (``GET /stores?name=``, ``POST /stores``) and the model versions of a store (``GET`` and ``POST``
``/stores/{id}/authorization-models``), over the same transport and the same key.
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
STORES_PAGE = 100            # the largest page ListStores allows (openfga/api: ListStoresRequest.page_size <= 100)


def _headers(token: SecretValue) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token.reveal()}"
    return headers


async def _call(http: httpx2.AsyncClient, method: str, url: str, body: dict[str, Any] | None, *, label: str,
                headers: dict[str, str], timeout: httpx2.Timeout, retry: bool) -> dict[str, Any]:
    """One request and its answer as a JSON object; the failure rules of the module docstring. ``label`` is the
    path as it is named in errors (never the full URL: the base carries nothing secret, but it stays short)."""
    for attempt in (1, 2):
        try:
            r = await http.request(method, url, json=body, headers=headers, timeout=timeout)
            break
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
            if retry and attempt == 1:
                continue  # the request never left: once more is safe
            raise EngineError(f"OpenFGA unreachable ({type(exc).__name__}) for {method} {label}") from exc
        except httpx2.HTTPError as exc:  # read timeouts, protocol errors: the request may have landed
            raise EngineError(f"OpenFGA request failed ({type(exc).__name__}) for {method} {label}") from exc
    if 200 <= r.status_code < 300:
        try:
            answer = r.json() if r.content else {}
        except ValueError as exc:
            raise EngineError(f"OpenFGA answered {method} {label} with a non-JSON body (HTTP {r.status_code})") from exc
        if not isinstance(answer, dict):
            raise EngineError(f"OpenFGA answered {method} {label} with a JSON {type(answer).__name__}, not an object")
        return answer
    code, message = _error(r)
    if r.status_code in (400, 422):
        raise InvalidRequest(f"OpenFGA refused {method} {label} ({code}): {message}")
    if r.status_code in (401, 403):
        raise EngineError(f"OpenFGA refused the credentials for {method} {label} (HTTP {r.status_code}, {code})")
    if r.status_code == 404:
        raise EngineError(f"OpenFGA store or model not found for {method} {label} (HTTP 404, {code})")
    raise EngineError(f"OpenFGA answered {method} {label} with HTTP {r.status_code} ({code})")


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
        return _headers(self.token)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, retry: bool) -> dict[str, Any]:
        return await _call(self.http, method, f"{self.base}/{path}", body, label=path, headers=self._headers(),
                           timeout=self.timeout, retry=retry)

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


class OpenFgaAdmin:
    """The store and its model versions, for ``inkcast authz write-model`` (E3.F3.9.S1). Reads are retried once on a
    connection error like the engine's; the two writes (a store, a model version) never are, because each one that
    lands is a new object."""

    def __init__(self, api_url: str, token: "SecretValue | str" = "", http: httpx2.AsyncClient | None = None,
                 timeout: float = 5.0):
        if not api_url:
            raise EngineError("OpenFGA needs INKCAST_FGA_API_URL")
        self.base = api_url.rstrip("/")
        self.token = SecretValue(token)
        self.http = http or httpx2.AsyncClient()
        self.timeout = httpx2.Timeout(timeout, connect=min(timeout, 3.0))

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, retry: bool) -> dict[str, Any]:
        return await _call(self.http, method, f"{self.base}/{path}", body, label=path.split("?")[0],
                           headers=_headers(self.token), timeout=self.timeout, retry=retry)

    async def find_store(self, name: str) -> str | None:
        """The id of the store called ``name``, or None. ListStores filters on the exact name; several stores may
        share one (OpenFGA does not forbid it), and that is refused rather than guessed at."""
        found: list[str] = []
        token = ""
        while True:
            query = f"stores?name={name}&page_size={STORES_PAGE}" + (f"&continuation_token={token}" if token else "")
            answer = await self._request("GET", query, retry=True)
            stores = answer.get("stores")
            if not isinstance(stores, list):
                raise EngineError("OpenFGA list-stores answered without stores")
            found += [str(s.get("id", "")) for s in stores if isinstance(s, dict) and s.get("name") == name]
            token = str(answer.get("continuation_token") or "")
            if not token:
                break
        if len(found) > 1:
            raise EngineError(f"OpenFGA holds {len(found)} stores named {name!r} ({', '.join(found)}): pin "
                              "INKCAST_FGA_STORE_ID to the one Inkcast uses and delete or rename the others")
        return found[0] if found else None

    async def create_store(self, name: str) -> str:
        answer = await self._request("POST", "stores", {"name": name}, retry=False)
        store_id = str(answer.get("id", ""))
        if not store_id:
            raise EngineError("OpenFGA create-store answered without an id")
        return store_id

    async def newest_model(self, store_id: str) -> dict[str, Any] | None:
        """The store's newest model version (OpenFGA lists them newest first), or None when it holds none."""
        answer = await self._request("GET", f"stores/{store_id}/authorization-models?page_size=1", retry=True)
        models = answer.get("authorization_models")
        if not isinstance(models, list):
            raise EngineError("OpenFGA read-authorization-models answered without authorization_models")
        if not models:
            return None
        if not isinstance(models[0], dict) or not models[0].get("id"):
            raise EngineError("OpenFGA read-authorization-models answered a model without an id")
        return models[0]

    async def write_model(self, store_id: str, model: dict[str, Any]) -> str:
        """A new model version from a compiled model (``authz/model.json``): its id."""
        body = {k: model[k] for k in ("schema_version", "type_definitions", "conditions") if k in model}
        answer = await self._request("POST", f"stores/{store_id}/authorization-models", body, retry=False)
        model_id = str(answer.get("authorization_model_id", ""))
        if not model_id:
            raise EngineError("OpenFGA write-authorization-model answered without an authorization_model_id")
        return model_id


def same_model(stored: dict[str, Any], model: dict[str, Any]) -> bool:
    """Whether a model version OpenFGA answers is the compiled ``model`` (``authz/model.json``).

    OpenFGA answers protobuf JSON with every field populated (v1.22.0, observed 2026-10-28: a computed relation's
    metadata comes back as ``{"directly_related_user_types": [], "module": "", "source_info": null}`` where the file
    has ``{}``; ``conditions: {}``, ``"metadata": null`` on a type without relations), and adds the ``id``. Both
    sides are compared with empty values dropped at every level, so only a difference in substance (a relation, a
    type, an allowed user type, a rewrite) tells them apart."""
    return _canonical(stored) == _canonical(model)


_EMPTY = (None, "", {}, [])


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "id":
                continue
            item = _canonical(item)
            if item not in _EMPTY:
                out[key] = item
        return out
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


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

# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The compiled authorization model (OpenFGA JSON, schema 1.1) and an evaluator of it over stored tuples
(E3.F3.6.S3).

``authz/model.fga`` is the source; ``fga model transform`` compiles it to ``authz/model.json``, and the copy
in this package (``model.json``, kept identical by tests/test_authz.py) ships in the image. ``Model`` reads the
type definitions: each relation is a *rewrite* tree of ``this`` (direct tuples), ``computedUserset`` (another
relation on the same object), ``tupleToUserset`` (a relation on the objects a tupleset relation points at),
``union``, ``intersection`` and ``difference``.

``Evaluator`` answers ``check`` and ``list_objects`` with OpenFGA's semantics over any ``TupleSource``: the
``LocalEngine`` gives it the ``authz_tuples`` table, the fake OpenFGA in tests an in-memory list. It is the one
place the rewrite semantics live, so both engines are tested against the same ``model.fga.yaml``.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Protocol

from .engine import MAX_DEPTH, MAX_LIST_OBJECTS
from .types import EngineError, InvalidRequest, Ref, Tuple, parse_user

PACKAGED_MODEL = pathlib.Path(__file__).with_name("model.json")


class TupleSource(Protocol):
    def users_of(self, obj: str, relation: str) -> list[str]:
        """The ``user`` of every stored tuple ``(user, relation, obj)``."""
        ...

    def objects_of_type(self, type_: str) -> list[str]:
        """Every distinct object of this type that any stored tuple names as its object."""
        ...


class Model:
    def __init__(self, data: dict[str, Any]):
        if data.get("schema_version") != "1.1":
            raise EngineError(f"unsupported authorization model schema {data.get('schema_version')!r} (need 1.1)")
        self.types: dict[str, dict[str, Any]] = {}
        for td in data.get("type_definitions", ()):
            self.types[td["type"]] = td
        self.id: str = str(data.get("id", ""))

    @classmethod
    def load(cls, path: "str | pathlib.Path | None" = None) -> "Model":
        """The packaged model, or the file ``INKCAST_AUTHZ_MODEL`` names (a deployment that pins its own copy)."""
        path = pathlib.Path(path) if path else PACKAGED_MODEL
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EngineError(f"authorization model {path} can't be loaded: {exc}") from exc

    def has_type(self, type_: str) -> bool:
        return type_ in self.types

    def has_relation(self, type_: str, relation: str) -> bool:
        return relation in self.types.get(type_, {}).get("relations", {})

    def relations(self, type_: str) -> set[str]:
        return set(self.types.get(type_, {}).get("relations", {}))

    def rewrite(self, type_: str, relation: str) -> dict[str, Any]:
        if type_ not in self.types:
            raise InvalidRequest(f"type '{type_}' not found in the authorization model")
        try:
            return self.types[type_]["relations"][relation]
        except KeyError:
            raise InvalidRequest(f"relation '{type_}#{relation}' not found in the authorization model") from None

    def assignable(self, type_: str, relation: str) -> list[dict[str, Any]]:
        """``directly_related_user_types`` of a relation: which user types (and usersets) tuples may name."""
        self.rewrite(type_, relation)
        meta = self.types[type_].get("metadata") or {}
        return list(((meta.get("relations") or {}).get(relation) or {}).get("directly_related_user_types") or ())

    def validate_tuple(self, t: Tuple) -> None:
        """What OpenFGA's Write refuses: an unknown type or relation, a relation nothing may be assigned to, a
        user type (or userset) the relation does not list, a userset relation that does not exist."""
        obj = Ref.parse(t.object)
        allowed = self.assignable(obj.type, t.relation)
        if not allowed:
            raise InvalidRequest(f"relation '{obj.type}#{t.relation}' is not directly assignable")
        utype, uid, urel = parse_user(t.user)
        if utype not in self.types:
            raise InvalidRequest(f"type '{utype}' not found in the authorization model")
        if urel and not self.has_relation(utype, urel):
            raise InvalidRequest(f"relation '{utype}#{urel}' not found in the authorization model")
        for entry in allowed:
            if entry.get("type") != utype or entry.get("relation") != urel:
                continue
            if uid == "*" and "wildcard" not in entry:
                continue
            if uid != "*" and "wildcard" in entry:
                continue
            return
        raise InvalidRequest(f"user '{t.user}' can't be assigned '{t.relation}' on '{obj.type}': "
                             f"the model allows {_describe(allowed)}")


def _describe(allowed: list[dict[str, Any]]) -> str:
    out = []
    for e in allowed:
        name = e.get("type", "?") + (f"#{e['relation']}" if e.get("relation") else "") + (":*" if "wildcard" in e else "")
        out.append(name)
    return ", ".join(out) or "nothing"


class Evaluator:
    """OpenFGA's check and list-objects semantics over a ``TupleSource``.

    A cycle in the data (a userset that, through other tuples, refers back to itself) contributes nothing, as in
    OpenFGA; a resolution deeper than ``MAX_DEPTH`` relations is an error rather than a wrong answer."""

    def __init__(self, model: Model, source: TupleSource):
        self.model, self.source = model, source

    # ---- check --------------------------------------------------------------------------------------------

    def check(self, user: str, relation: str, obj: str) -> bool:
        utype, _, urel = parse_user(user)
        ref = Ref.parse(obj)
        if not self.model.has_type(utype):
            raise InvalidRequest(f"type '{utype}' not found in the authorization model")
        if urel and not self.model.has_relation(utype, urel):
            raise InvalidRequest(f"relation '{utype}#{urel}' not found in the authorization model")
        self.model.rewrite(ref.type, relation)
        return self._eval(user, relation, obj, 0, frozenset())

    def _eval(self, user: str, relation: str, obj: str, depth: int, path: frozenset) -> bool:
        if depth > MAX_DEPTH:
            raise EngineError(f"authorization check too deep: more than {MAX_DEPTH} relations followed "
                              f"resolving {relation} on {obj}")
        key = (user, relation, obj)
        if key in path:
            return False
        rewrite = self.model.rewrite(Ref.parse(obj).type, relation)
        return self._node(rewrite, user, relation, obj, depth, path | {key})

    def _node(self, node: dict[str, Any], user: str, relation: str, obj: str, depth: int, path: frozenset) -> bool:
        if "this" in node:
            return self._direct(user, relation, obj, depth, path)
        if "computedUserset" in node:
            return self._eval(user, node["computedUserset"]["relation"], obj, depth + 1, path)
        if "tupleToUserset" in node:
            return self._tuple_to_userset(node["tupleToUserset"], user, obj, depth, path)
        if "union" in node:
            return any(self._node(c, user, relation, obj, depth, path) for c in node["union"]["child"])
        if "intersection" in node:
            return all(self._node(c, user, relation, obj, depth, path) for c in node["intersection"]["child"])
        if "difference" in node:
            d = node["difference"]
            return self._node(d["base"], user, relation, obj, depth, path) and \
                not self._node(d["subtract"], user, relation, obj, depth, path)
        raise EngineError(f"unknown rewrite {sorted(node)} on {relation}")

    def _direct(self, user: str, relation: str, obj: str, depth: int, path: frozenset) -> bool:
        utype, _, urel = parse_user(user)
        for stored in self.source.users_of(obj, relation):
            if stored == user:
                return True
            stype, sid, srel = parse_user(stored)
            if sid == "*" and srel is None:
                if stype == utype and urel is None:  # user:* grants every user, not a userset
                    return True
                continue
            if srel and self._eval(user, srel, f"{stype}:{sid}", depth + 1, path):
                return True
        return False

    def _tuple_to_userset(self, node: dict[str, Any], user: str, obj: str, depth: int, path: frozenset) -> bool:
        tupleset = node["tupleset"]["relation"]
        computed = node["computedUserset"]["relation"]
        for parent in self.source.users_of(obj, tupleset):
            ptype, pid, prel = parse_user(parent)
            if prel or pid == "*" or not self.model.has_relation(ptype, computed):
                continue  # a tupleset names plain objects; a type without the relation contributes nothing
            if self._eval(user, computed, parent, depth + 1, path):
                return True
        return False

    # ---- list objects -------------------------------------------------------------------------------------

    def list_objects(self, user: str, relation: str, type_: str) -> list[str]:
        """Correct, not clever: every object of the type that any tuple names, checked one by one, up to the cap.
        Every relation bottoms out in a tuple on its own object, so nothing else can qualify."""
        self.model.rewrite(type_, relation)
        found: list[str] = []
        for obj in sorted(set(self.source.objects_of_type(type_))):
            if self.check(user, relation, obj):
                found.append(obj)
                if len(found) >= MAX_LIST_OBJECTS:
                    break
        return found

# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The vocabulary of authorization (E3.F3.6.S1): references, tuples, relation names, errors.

Everything an engine takes or returns is spelled here, so that routes, MCP tools, the outbox (E3.F3.8) and
tests never build ``"type:id"`` strings by hand. The identifier rules are OpenFGA's own (an object id has no
``:``, ``#`` or whitespace; a type and a relation name have no ``:``, ``#``, ``@`` or whitespace), checked at
construction so a malformed id fails here, in the caller, and never reaches a store as a tuple nobody can
delete through the API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# OpenFGA's limits: an object (``type:id``) is at most 256 characters, a type name 254, a relation 50.
# No control characters either (OpenFGA's unicode.IsControl: C0, DEL, C1), and \Z, not $, which would let a
# trailing newline through.
_TYPE = re.compile(r"^[^:#@\s\x00-\x1f\x7f-\x9f]{1,254}\Z")
_RELATION = re.compile(r"^[^:#@\s\x00-\x1f\x7f-\x9f]{1,50}\Z")
_ID = re.compile(r"^[^:#\s\x00-\x1f\x7f-\x9f]{1,256}\Z")
_USER = re.compile(r"^(?P<type>[^:#@\s\x00-\x1f\x7f-\x9f]{1,254}):(?P<id>[^:#\s\x00-\x1f\x7f-\x9f]{1,256}|\*)"
                   r"(?:#(?P<relation>[^:#@\s\x00-\x1f\x7f-\x9f]{1,50}))?\Z")

# Read-after-write checks (grant, then act) ask for higher consistency; everything else takes the cache.
Consistency = Literal["minimize_latency", "higher_consistency"]
MINIMIZE_LATENCY: Consistency = "minimize_latency"
HIGHER_CONSISTENCY: Consistency = "higher_consistency"


class EngineError(RuntimeError):
    """The engine could not answer: a timeout, a 5xx, bad configuration, an unknown type or relation. Callers
    fail closed (``require()`` answers 503; nothing is granted on an error)."""


class InvalidRequest(EngineError):
    """The request itself is wrong (OpenFGA's 400s): an unknown relation, a user type the model does not allow
    on that relation, a duplicate write. Still an ``EngineError`` so that every caller fails closed."""


@dataclass(frozen=True, slots=True)
class Ref:
    """One object: ``Ref("post", "post_1")`` is ``post:post_1``."""

    type: str
    id: str

    def __post_init__(self) -> None:
        if not _TYPE.match(self.type):
            raise ValueError(f"invalid object type {self.type!r}")
        if not _ID.match(self.id):
            raise ValueError(f"invalid object id {self.id!r} (no ':', '#' or whitespace; 1-256 characters)")
        if len(self.type) + 1 + len(self.id) > 256:
            raise ValueError(f"invalid object {self.type}:{self.id[:32]}...: 'type:id' is at most 256 characters")

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"

    @classmethod
    def parse(cls, text: str) -> "Ref":
        type_, sep, id_ = text.partition(":")
        if not sep:
            raise ValueError(f"an object is 'type:id', got {text!r}")
        return cls(type_, id_)


def user_ref(user_id: str) -> str:
    """The subject string for an Inkcast user id (ADR-0009: ``user:<id>``, never an email)."""
    return str(Ref("user", user_id))


def parse_user(user: str) -> tuple[str, str, str | None]:
    """``type:id`` or ``type:id#relation`` (a userset such as ``organization:acme#member``) or ``type:*``;
    returns (type, id, relation or None). Raises ``ValueError`` for anything else."""
    m = _USER.match(user)
    if not m or len(user.encode("utf-8")) > 512:  # OpenFGA's user limit is 512 bytes
        raise ValueError(f"invalid user {user!r}: expected 'type:id', 'type:id#relation' or 'type:*'")
    return m.group("type"), m.group("id"), m.group("relation")


@dataclass(frozen=True, slots=True)
class Tuple:
    """One relationship: ``user`` has ``relation`` on ``object``. The user may be a userset
    (``organization:acme#member``: everyone who is a member of Acme)."""

    user: str
    relation: str
    object: str

    def __post_init__(self) -> None:
        parse_user(self.user)
        if not _RELATION.match(self.relation):
            raise ValueError(f"invalid relation {self.relation!r}")
        Ref.parse(self.object)

    @classmethod
    def of(cls, user: str, relation: str, obj: "Ref | str") -> "Tuple":
        return cls(user, relation, str(obj))

    @property
    def ref(self) -> Ref:
        return Ref.parse(self.object)

    def as_key(self) -> dict[str, str]:
        """The OpenFGA ``tuple_key`` shape."""
        return {"user": self.user, "relation": self.relation, "object": self.object}

    @classmethod
    def from_key(cls, key: dict) -> "Tuple":
        return cls(str(key["user"]), str(key["relation"]), str(key["object"]))


# ---- the relations of authz/model.fga, by type ------------------------------------------------------------
# Hand-written, so that a typo in a route is an AttributeError; tests/test_authz.py fails when they drift from
# the compiled model (authz/model.json). Add a relation there first, then here.


class Organization:
    TYPE = "organization"
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    BILLING_ADMIN = "billing_admin"
    CAN_MANAGE_BILLING = "can_manage_billing"
    CAN_VIEW_BILLING = "can_view_billing"
    CAN_MANAGE_MEMBERS = "can_manage_members"
    CAN_MANAGE_SECURITY = "can_manage_security"
    CAN_VIEW_AUDIT = "can_view_audit"
    CAN_CREATE_WORKSPACE = "can_create_workspace"
    CAN_MANAGE_CUSTOM_ROLES = "can_manage_custom_roles"


class Role:
    TYPE = "role"
    ORG = "org"
    ASSIGNEE = "assignee"


class Workspace:
    TYPE = "workspace"
    ORG = "org"
    ORG_MEMBER = "org_member"
    ROLE_MANAGER = "role_manager"
    ROLE_EDITOR = "role_editor"
    ROLE_REVIEWER = "role_reviewer"
    ROLE_VIEWER = "role_viewer"
    MANAGER = "manager"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    VIEWER = "viewer"
    CLIENT_REVIEWER = "client_reviewer"
    CAN_MANAGE_MEMBERS = "can_manage_members"
    CAN_MANAGE_CHANNELS = "can_manage_channels"
    CAN_CREATE_POST = "can_create_post"
    CAN_CREATE_TOKEN = "can_create_token"
    CAN_MANAGE_WORKSPACE_TOKENS = "can_manage_workspace_tokens"
    CAN_VIEW_USAGE = "can_view_usage"


class Channel:
    TYPE = "channel"
    WORKSPACE = "workspace"
    ROLE_PUBLISHER = "role_publisher"
    PUBLISHER = "publisher"
    CAN_PUBLISH = "can_publish"


class Post:
    TYPE = "post"
    WORKSPACE = "workspace"
    AUTHOR = "author"
    CAN_EDIT = "can_edit"
    CAN_APPROVE = "can_approve"
    CAN_VIEW = "can_view"
    CAN_CLIENT_REVIEW = "can_client_review"


RELATIONS: dict[str, type] = {c.TYPE: c for c in (Organization, Role, Workspace, Channel, Post)}  # type: ignore[attr-defined]


def relations_of(cls: type) -> set[str]:
    """The relation names a constants class declares (everything but ``TYPE``)."""
    return {v for k, v in vars(cls).items() if k.isupper() and k != "TYPE"}

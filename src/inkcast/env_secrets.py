# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``inkcast env-secrets`` (E3.F3.9.S7, issue #323): the cluster makes every generated Secret of a lab environment.

    inkcast env-secrets          # in a pod in inkcast-staging or inkcast-prod, with a token for the Kubernetes API

It runs as the init container ``env-secrets`` of OpenFGA's pod in the environments (deploy/kustomize/components/
env-secrets), so it runs at every start of that pod (a fresh environment, every release, since the container runs the
release's image, a restart or an eviction) and always before OpenFGA reads its Secrets. For the side its namespace
names (inkcast-staging is staging, inkcast-prod is prod), it writes:

    inkcast-db/inkcast-pg-openfga-<side>  kubernetes.io/basic-auth   username, password: the managed role's password
                                                                     (deploy/kustomize/shared/inkcast-db/cluster.yaml)
    <namespace>/openfga-db-app            uri, username, password    what OpenFGA's server and migrate Job connect with
    keycloak/inkcast-<side>-realm         identity-client-secret,    the realm import's placeholders
                                          sync-client-secret         (deploy/kustomize/shared/keycloak-realms)
    <namespace>/inkcast-secrets           secret-key, fga-api-token, what Inkcast mounts, and OpenFGA's preshared key
                                          oidc-client-secret,
                                          keycloak-sync-secret

Three pairs hold one value on both sides: the role's password (the role Secret and openfga-db-app, whose uri carries
it), the identity client's secret (identity-client-secret and oidc-client-secret) and the sync client's
(sync-client-secret and keycloak-sync-secret). For each pair, and for Inkcast's key and OpenFGA's:

- absent everywhere: one value from the CSPRNG (``secrets``), written to every side;
- present on one side: copied to the other;
- present on both and equal: kept; present on both and different: the run fails naming the pair.

An existing value is never replaced: a Secret that exists gets only the keys it lacks, by a merge patch conditional on
the resourceVersion read. Everything is read and checked before anything is written, and any problem (a pair that
disagrees, a Secret its consumer could not read) stops the run with nothing written. A write that loses a race to
another writer (409) starts the run over from the reads, so two pods starting at once end on the same values. A
crash between two writes leaves one side of a pair, which the next run copies. The output names Secrets and keys,
never a value. Exit 0 when every Secret is present and agrees, 1 otherwise, with the reason on stderr.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import secrets
import ssl
import string
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

import httpx2

from .libs.secrets.checks import _PLACEHOLDER_KEYS

SERVICE_ACCOUNT = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")
SIDES = {"inkcast-staging": "staging", "inkcast-prod": "prod"}
DB_NAMESPACE, KEYCLOAK_NAMESPACE = "inkcast-db", "keycloak"
DB_HOST, DB_PORT = "inkcast-pg-rw.inkcast-db.svc", 5432
PART_OF = {"app.kubernetes.io/part-of": "inkcast"}
# The keys that must agree: (the realm Secret's, inkcast-secrets'), the identity client's then the sync client's.
CLIENT_PAIRS = (("identity-client-secret", "oidc-client-secret"), ("sync-client-secret", "keycloak-sync-secret"))
# What a realm client secret must not hold: kc.sh import substitutes the value into the realm's JSON text, so a quote,
# a backslash or a control character breaks the import, and ${...} in it is expanded from the import Job's environment
# (a copy of the Keycloak server's, with its database password): measured on Keycloak 26.6.1 in PR #325's review.
UNSAFE_IN_REALM = frozenset('"\\${}') | frozenset(map(chr, range(0x20))) | {"\x7f"}
ATTEMPTS = 5  # rounds of read, check, write when other writers keep winning races
# Which kustomization grants this run its access in each namespace: named when the API refuses a call.
GRANTS = {DB_NAMESPACE: "deploy/kustomize/shared/inkcast-db (rbac.yaml)",
          KEYCLOAK_NAMESPACE: "deploy/kustomize/shared/keycloak-access"}
# Each side creates only its own Secrets: RBAC can't narrow `create` to a name, so this policy does (review C1, #323).
ADMISSION = "deploy/kustomize/shared/env-secrets-admission"
ALPHANUMERIC = string.ascii_letters + string.digits


def _alphanumeric(length: int) -> str:
    return "".join(secrets.choice(ALPHANUMERIC) for _ in range(length))


def make_keycloak_client() -> str:
    """Keycloak's own format for a client authenticated by a secret: 32 alphanumerics, about 190 bits
    (KeycloakModelUtils.generateSecret and SecretGenerator.randomString at 26.6.7). Nothing in UNSAFE_IN_REALM, nothing
    to escape in a form body. Drawn per side: both realms have the same client ids, so a staging value that equalled
    prod's would sign in to prod."""
    return _alphanumeric(32)


def make_preshared() -> str:
    """OpenFGA's preshared key, which Inkcast sends as a bearer token: 64 alphanumerics. No comma (the server reads
    OPENFGA_AUTHN_PRESHARED_KEYS as a comma-separated list: viper v1.20.1's default decode hook) and no whitespace."""
    return _alphanumeric(64)


def make_role_password() -> str:
    """The PostgreSQL role's password: 48 hex characters, 192 bits, nothing to percent-encode in the URI."""
    return secrets.token_hex(24)


def make_server_key() -> str:
    """INKCAST_SECRET_KEY: 64 URL-safe characters, as Config.validate's own hint makes one (token_urlsafe(48)), drawn
    again until the posture check passes it (libs/secrets/checks.py: no placeholder word such as "test" or "1234",
    at least 16 distinct characters), so production never refuses a generated key."""
    while True:
        value = secrets.token_urlsafe(48)
        if len(set(value)) >= 16 and not any(word in value.lower() for word in _PLACEHOLDER_KEYS):
            return value


class Refusal(Exception):
    """This run can't start (not in an environment's pod), or can't finish (the API refused or failed a call)."""


@dataclass(frozen=True)
class Ref:
    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class Layout:
    """Every Secret of one side, by the names its consumers read."""

    namespace: str
    side: str

    @classmethod
    def for_namespace(cls, namespace: str) -> "Layout":
        if namespace not in SIDES:
            raise Refusal(f"runs in inkcast-staging or inkcast-prod, the lab's environments; this pod is in "
                          f"{namespace or 'no namespace'}.")
        return cls(namespace, SIDES[namespace])

    @property
    def role(self) -> str:
        return f"openfga_{self.side}"  # the managed role, and the database it owns (databases.yaml)

    @property
    def db_role(self) -> Ref:
        return Ref(DB_NAMESPACE, f"inkcast-pg-openfga-{self.side}")

    @property
    def db_app(self) -> Ref:
        return Ref(self.namespace, "openfga-db-app")

    @property
    def realm(self) -> Ref:
        return Ref(KEYCLOAK_NAMESPACE, f"inkcast-{self.side}-realm")

    @property
    def app(self) -> Ref:
        return Ref(self.namespace, "inkcast-secrets")

    def uri(self, password: str) -> str:
        """What OpenFGA connects with: always TLS (the server admits the role over TLS only), to the role's own
        database. A password written by hand may need escaping; a generated one (hex) never does."""
        return (f"postgres://{self.role}:{quote(password, safe='')}@{DB_HOST}:{DB_PORT}/{self.role}"
                f"?sslmode=require")


@dataclass
class Stored:
    data: dict[str, str]
    version: str
    unreadable: tuple[str, ...] = ()  # keys whose value is not UTF-8 text


@dataclass
class Write:
    ref: Ref
    values: dict[str, str]
    origins: dict[str, str]  # key -> "generated", "from <ref> <key>" or "the same as <ref> <key>": never a value
    version: str | None = None  # None: create the Secret; else add these keys to the version read
    type: str = "Opaque"
    labels: dict[str, str] = field(default_factory=lambda: dict(PART_OF))


class Kube:
    """The few Secret calls this needs, over the pod's own credentials (``connect``)."""

    def __init__(self, http: httpx2.Client):
        self.http = http
        self.seen: set[str] = set()  # every value read or sent, as text and as the API's base64: cut from its errors

    def read(self, ref: Ref) -> Stored | None:
        answer = self.http.get(f"/api/v1/namespaces/{ref.namespace}/secrets/{ref.name}")
        if answer.status_code == 404:
            return None
        if answer.status_code != 200:
            raise _refused("read", ref, answer, self.seen)
        obj = answer.json()
        data, unreadable = {}, []
        for key, encoded in (obj.get("data") or {}).items():
            self.seen.add(encoded)
            try:
                data[key] = base64.b64decode(encoded).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                unreadable.append(key)
        self.seen.update(data.values())
        return Stored(data, obj["metadata"]["resourceVersion"], tuple(unreadable))

    def write(self, change: Write) -> bool:
        """Create the Secret, or add keys to the version read. False when another writer got there first (409)."""
        encoded = {key: base64.b64encode(value.encode()).decode() for key, value in change.values.items()}
        self.seen.update(change.values.values(), encoded.values())
        if change.version is None:
            answer = self.http.post(f"/api/v1/namespaces/{change.ref.namespace}/secrets", json={
                "apiVersion": "v1", "kind": "Secret", "type": change.type,
                "metadata": {"name": change.ref.name, "labels": change.labels}, "data": encoded})
            verb, done = "create", (200, 201)
        else:
            answer = self.http.patch(
                f"/api/v1/namespaces/{change.ref.namespace}/secrets/{change.ref.name}",
                content=json.dumps({"metadata": {"resourceVersion": change.version}, "data": encoded}),
                headers={"Content-Type": "application/merge-patch+json"})
            verb, done = "patch", (200,)
        if answer.status_code == 409:
            return False
        if answer.status_code not in done:
            raise _refused(verb, change.ref, answer, self.seen)
        return True


def _refused(verb: str, ref: Ref, answer: httpx2.Response, seen: Iterable[str] = ()) -> Refusal:
    """The API's own answer, never a body that could hold data: a Status carries the reason and a message. A webhook
    that denies a write may quote the object in them, so every value this run read or sent is cut out first."""
    try:
        body = answer.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or body.get("kind") != "Status":
        body = {}
    reason, message = (str(body.get(key) or "") for key in ("reason", "message"))
    for value in sorted(filter(None, seen), key=len, reverse=True):
        reason, message = reason.replace(value, "…"), message.replace(value, "…")
    text = f"{verb} Secret {ref}: HTTP {answer.status_code} {reason}".rstrip()
    if message:
        text += f": {message}"
    if answer.status_code == 403 and "ValidatingAdmissionPolicy" in message:
        text += f". An admission policy refused it; this command's is in {ADMISSION}."
    elif answer.status_code == 403 and "admission webhook" in message:
        text += ". An admission webhook refused it, not RBAC: its message above says why."
    elif answer.status_code == 403:
        grant = GRANTS.get(ref.namespace, "deploy/kustomize/components/env-secrets (rbac.yaml)")
        text += f". The Role that allows it is in {grant}: apply it."
    return Refusal(text)


@dataclass
class Plan:
    writes: list[Write] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def decide(layout: Layout, stored: Mapping[Ref, Stored | None]) -> Plan:
    """What to write, from what is stored: pure, so every case is a unit test. Any problem means no write at all."""
    plan = Plan()

    def value(ref: Ref, key: str) -> str | None:
        """The stored value, or None when absent. An empty or unreadable one is a problem (and nothing is written)."""
        found = stored[ref]
        if found is None:
            return None
        if key in found.unreadable:
            plan.problems.append(f"{ref} {key} is not UTF-8 text: no consumer can read it.")
            return None
        if key in found.data and not found.data[key]:
            plan.problems.append(f"{ref} {key} is empty.")
            return None
        return found.data.get(key)

    # The role's password: the role Secret (CloudNativePG's input) and openfga-db-app (OpenFGA's), whose uri carries it.
    role_side = app_side = None
    if stored[layout.db_role] is not None:
        username, password = value(layout.db_role, "username"), value(layout.db_role, "password")
        if username != layout.role or not password:
            plan.problems.append(f"{layout.db_role} is not the basic-auth Secret of role {layout.role} (username "
                                 f"{layout.role} and a password).")
        else:
            role_side = password
    if stored[layout.db_app] is not None:
        username, password, uri = (value(layout.db_app, key) for key in ("username", "password", "uri"))
        if username != layout.role or not password or uri != layout.uri(password):
            plan.problems.append(f"{layout.db_app} is not role {layout.role}'s connection Secret (username, password and "
                                 f"uri postgres://{layout.role}:…@{DB_HOST}:{DB_PORT}/{layout.role}?sslmode=require).")
        else:
            app_side = password
    if role_side and app_side and role_side != app_side:
        plan.problems.append(f"{layout.db_app} and {layout.db_role} hold different passwords for role {layout.role}. The "
                             f"role follows {layout.db_role}: delete {layout.db_app}, and the next start of OpenFGA's "
                             "pod copies it.")
    password = role_side or app_side or make_role_password()
    role_origin = f"from {layout.db_app}" if app_side else "generated"
    app_origin = f"from {layout.db_role}" if role_side else f"the same as {layout.db_role}"

    # The two Keycloak clients: the realm Secret (the import's placeholders) and inkcast-secrets (what Inkcast sends).
    realm_new: dict[str, tuple[str, str]] = {}
    app_new: dict[str, tuple[str, str]] = {}
    for realm_key, app_key in CLIENT_PAIRS:
        in_realm, in_app = value(layout.realm, realm_key), value(layout.app, app_key)
        bound = in_realm or in_app  # what the realm Secret holds, or would once copied
        if bound and UNSAFE_IN_REALM.intersection(bound):
            where = f"{layout.realm} {realm_key}" if in_realm else f"{layout.app} {app_key}"
            plan.problems.append(
                f"{where} holds a character the realm import cannot take: a double quote, a backslash, $, {{, }} or a "
                "control character (kc.sh import substitutes the value into the realm's JSON and expands ${...} in it). "
                "Put a value without them on both sides, and on the client in Keycloak if the realm is imported.")
        elif in_realm and in_app and in_realm != in_app:
            plan.problems.append(
                f"{layout.realm} {realm_key} and {layout.app} {app_key} disagree. Keycloak's realm holds what "
                f"{layout.realm} held when it was imported: remove the key from the Secret that is wrong, and the next "
                "start of OpenFGA's pod copies the other.")
        elif in_realm and not in_app:
            app_new[app_key] = (in_realm, f"from {layout.realm} {realm_key}")
        elif in_app and not in_realm:
            realm_new[realm_key] = (in_app, f"from {layout.app} {app_key}")
        elif not in_realm and not in_app:
            fresh = make_keycloak_client()
            realm_new[realm_key] = (fresh, "generated")
            app_new[app_key] = (fresh, f"the same as {layout.realm} {realm_key}")
    # Held only in inkcast-secrets: Inkcast's key, and OpenFGA's preshared key (both pods read the one entry).
    for app_key, make in (("secret-key", make_server_key), ("fga-api-token", make_preshared)):
        if value(layout.app, app_key) is None:
            app_new[app_key] = (make(), "generated")

    if plan.problems:
        return plan
    # The side a pair is decided on first, then the side that copies it: a crash in between leaves one side, which
    # the next run copies, never two sides that disagree.
    if stored[layout.db_role] is None:
        plan.writes.append(Write(layout.db_role, {"username": layout.role, "password": password},
                                 {"username": layout.role, "password": role_origin}, type="kubernetes.io/basic-auth",
                                 labels={**PART_OF, "cnpg.io/reload": "true"}))
    _add(plan, layout.realm, stored[layout.realm], realm_new)
    if stored[layout.db_app] is None:
        plan.writes.append(Write(layout.db_app, {"uri": layout.uri(password), "username": layout.role, "password": password},
                                 {"uri": "with that password", "username": layout.role, "password": app_origin}))
    _add(plan, layout.app, stored[layout.app], app_new)
    for ref in (layout.db_role, layout.realm, layout.db_app, layout.app):
        if stored[ref] is not None and ref not in {w.ref for w in plan.writes}:
            plan.kept.append(f"{ref}: kept ({', '.join(sorted(stored[ref].data))})")
    return plan


def _add(plan: Plan, ref: Ref, found: Stored | None, new: dict[str, tuple[str, str]]) -> None:
    """Create the Secret with these keys, or add them to the version read."""
    if not new:
        return
    plan.writes.append(Write(ref, {key: v for key, (v, _) in new.items()}, {key: o for key, (_, o) in new.items()},
                             version=None if found is None else found.version))


def describe(change: Write) -> str:
    """One line per write: the Secret, what was done, and where each key came from. Never a value."""
    keys = ", ".join(f"{key} ({change.origins[key]})" for key in change.values)
    return f"{change.ref}: {'created' if change.version is None else 'added'} {keys}"


def ensure(kube: Kube, layout: Layout, say: Callable[[str], None]) -> list[str]:
    """Read, check, write; again from the reads when another writer won a race. Returns the problems (none: done)."""
    refs = (layout.db_role, layout.db_app, layout.realm, layout.app)
    for _ in range(ATTEMPTS):
        plan = decide(layout, {ref: kube.read(ref) for ref in refs})
        if plan.problems:
            return [*plan.problems, "Nothing was written."]
        for line in plan.kept:
            say(line)
        for change in plan.writes:
            if not kube.write(change):
                say(f"{change.ref}: another writer got there first; reading again.")
                break
            say(describe(change))
        else:
            return []
    return [f"Another writer kept changing these Secrets through {ATTEMPTS} rounds; nothing more was written. The "
            "next start of OpenFGA's pod tries again."]


def connect(environ: Mapping[str, str], root: pathlib.Path = SERVICE_ACCOUNT) -> httpx2.Client:
    """The API server, as client-go's in-cluster configuration reaches it: KUBERNETES_SERVICE_HOST and _PORT (set by
    the kubelet in every container), the projected token and the cluster's CA under ``root``. Never through a proxy."""
    host = environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    if not host:
        raise Refusal("KUBERNETES_SERVICE_HOST is not set: this runs in a pod, as OpenFGA's init container.")
    port = environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    try:
        bearer = (root / "token").read_text().strip()
        verify = ssl.create_default_context(cafile=str(root / "ca.crt"))
    except OSError as exc:
        raise Refusal(f"can't read the pod's credentials: {exc.strerror}: {exc.filename}") from None
    host = f"[{host}]" if ":" in host else host
    return httpx2.Client(base_url=f"https://{host}:{port}", headers={"Authorization": f"Bearer {bearer}"},
                         verify=verify, timeout=15, trust_env=False)


def env_secrets_main(argv: list[str], *, root: pathlib.Path = SERVICE_ACCOUNT, environ: Mapping[str, str] | None = None,
                     connect: Callable[[Mapping[str, str], pathlib.Path], httpx2.Client] = connect,
                     out: Any = None, err: Any = None) -> int:
    argparse.ArgumentParser(prog="inkcast env-secrets", description=__doc__.split("\n\n")[0]).parse_args(argv)
    out, err = out or sys.stdout, err or sys.stderr
    environ = os.environ if environ is None else environ
    try:
        namespace = (root / "namespace").read_text().strip() if (root / "namespace").exists() else ""
        layout = Layout.for_namespace(namespace)
        with connect(environ, root) as http:
            problems = ensure(Kube(http), layout, lambda line: print(line, file=out, flush=True))
    except Refusal as exc:
        print(f"inkcast env-secrets: {exc}", file=err)
        return 1
    except httpx2.HTTPError as exc:
        print(f"inkcast env-secrets: can't reach the Kubernetes API: {type(exc).__name__}: {exc}", file=err)
        return 1
    for problem in problems:
        print(f"inkcast env-secrets: {problem}", file=err)
    if problems:
        return 1
    print(f"{layout.namespace}: every generated Secret is present and in agreement.", file=out)
    return 0

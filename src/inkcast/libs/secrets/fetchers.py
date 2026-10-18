# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Where secrets come from. Every source implements the same small ``Fetcher`` interface, so the
caching ``SecretsProvider`` on top does not care which one is in use.

=================  ==========  ========================================================================
``EnvFetcher``     ``env``     ``NAME_FILE`` (a mounted file, re-read on rotation) then ``NAME`` (a variable)
``DirectoryFetcher`` ``dir``   ``INKCAST_SECRETS_DIR/<name>``: a Kubernetes Secret volume (``secret-key``,
                               ``oidc-client-secret``...), the Vault Agent injector or ESO's mounted output
``AgentFetcher``   ``agent``   the AWS Workload Credentials Provider (Secrets Manager Agent) on loopback
``AwsSdkFetcher``  ``aws``     Secrets Manager through boto3 (optional; not installed in the default image)
``VaultFetcher``   ``vault``   OpenBao/Vault KV v2 (reserved; use ``dir`` with the agent injector today)
``ChainFetcher``   ``chain``   the first source that has the secret wins
=================  ==========  ========================================================================

``fetcher_from_environ`` builds the chain for a process: always env then ``INKCAST_SECRETS_DIR`` (when
set), then the remote back-end named by ``INKCAST_SECRETS_SOURCE``.

To add a source: subclass ``Fetcher``, implement ``fetch`` (return ``None`` when you don't have the
secret, raise ``SecretFetchError`` when you could not ask), and register it in ``BACKENDS``.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping
from urllib.parse import urlsplit

from .naming import SecretNaming, secret_slug


class SecretFetchError(RuntimeError):
    """The source could not be asked (network, permission, agent down). Stale-on-error applies."""


class NotInstalledError(SecretFetchError):
    """A back-end whose client library or configuration is not available in this build."""


@dataclass(frozen=True)
class Fetched:
    value: str
    source: str  # e.g. "env", "file:/var/run/secrets/inkcast/secret-key", "agent:/inkcast/prod/secret-key"
    version: str | None = None  # the source's own version id, or a file's mtime+size, for change detection


class Fetcher(ABC):
    kind: ClassVar[str] = "abstract"

    @abstractmethod
    def fetch(self, name: str, *, force: bool = False) -> Fetched | None:
        """The secret named ``name`` (a setting name such as ``OIDC_CLIENT_SECRET``), or ``None`` if this
        source does not have it. ``force`` bypasses any cache the source itself keeps."""

    def describe(self, name: str) -> dict[str, Any] | None:
        """Remote metadata without the value (rotation dates, current version), when the source has any."""
        return None

    def paths(self, name: str) -> list[str]:
        """Files on disk that hold this secret, for the file-permission posture check."""
        return []


def _file_version(path: str) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


class EnvFetcher(Fetcher):
    """``NAME_FILE`` (preferred: a mounted file is re-read, so rotation needs no restart) then ``NAME``."""

    kind = "env"

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._env = environ if environ is not None else os.environ

    def fetch(self, name: str, *, force: bool = False) -> Fetched | None:
        path = self._env.get(f"{name}_FILE", "").strip()
        if path:
            with open(path, encoding="utf-8") as f:  # OSError propagates: a dangling NAME_FILE is a config error
                return Fetched(f.read().strip(), f"file:{path}", _file_version(path))
        value = self._env.get(name, "").strip()
        return Fetched(value, "env") if value else None

    def paths(self, name: str) -> list[str]:
        path = self._env.get(f"{name}_FILE", "").strip()
        return [path] if path else []


class DirectoryFetcher(Fetcher):
    """A directory of one file per secret, as a Kubernetes Secret volume mounts it.

    ``OIDC_CLIENT_SECRET`` is looked up as ``OIDC_CLIENT_SECRET``, ``oidc_client_secret`` and
    ``oidc-client-secret``; ``INKCAST_SECRET_KEY`` also as ``secret-key`` (the ``INKCAST_`` prefix dropped),
    which is how deploy/kustomize names the keys of the ``inkcast-secrets`` Secret.
    """

    kind = "dir"

    def __init__(self, directory: str):
        self.directory = Path(directory)

    def candidates(self, name: str) -> list[Path]:
        names = [name, name.lower(), name.lower().replace("_", "-"), secret_slug(name)]
        seen: list[Path] = []
        for n in names:
            p = self.directory / n
            if p not in seen:
                seen.append(p)
        return seen

    def fetch(self, name: str, *, force: bool = False) -> Fetched | None:
        for path in self.candidates(name):
            if path.is_file():
                return Fetched(path.read_text(encoding="utf-8").strip(), f"file:{path}", _file_version(str(path)))
        return None

    def paths(self, name: str) -> list[str]:
        return [str(p) for p in self.candidates(name) if p.is_file()]


_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _single_value(secret_string: str, name: str) -> str:
    """A remote secret is one value: a plain string, or a JSON object with one key or a ``value`` key."""
    try:
        parsed = json.loads(secret_string)
    except ValueError:
        return secret_string
    if isinstance(parsed, dict):
        if "value" in parsed:
            return str(parsed["value"])
        if len(parsed) == 1:
            return str(next(iter(parsed.values())))
        raise SecretFetchError(f"secret {name} is a JSON object with several keys; Inkcast secrets hold one value "
                               "(use a 'value' key)")
    return secret_string


class AgentFetcher(Fetcher):
    """The AWS Workload Credentials Provider (formerly the Secrets Manager Agent) on ``http://localhost:2773``.

    The agent holds the AWS credentials and its own cache; the app proves it is a local process by
    sending the SSRF token from ``INKCAST_AGENT_TOKEN_FILE`` (``/var/run/awssmatoken``). The token is read on
    every call because the agent regenerates it when it restarts, and it is only ever sent to loopback.
    """

    kind = "agent"

    def __init__(self, naming: SecretNaming, url: str = "http://localhost:2773", token_file: str = "/var/run/awssmatoken",
                 timeout: float = 3.0, http: Any = None):
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname not in _LOOPBACK:
            raise SecretFetchError(f"INKCAST_AGENT_URL must be http://localhost:<port> (got {url!r}): the SSRF token "
                                   "must never leave the host")
        self.naming, self.url, self.token_file, self.timeout = naming, url.rstrip("/"), token_file, timeout
        self._http = http

    def _client(self) -> Any:
        if self._http is None:
            import httpx2

            self._http = httpx2.Client(timeout=self.timeout, follow_redirects=False)
        return self._http

    def fetch(self, name: str, *, force: bool = False) -> Fetched | None:
        secret_id = self.naming.remote_id(name)
        try:
            token = Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SecretFetchError(f"can't read the agent's SSRF token {self.token_file}: {exc}") from exc
        params = {"secretId": secret_id}
        if force:
            params["refreshNow"] = "true"  # bypass the agent's own cache
        try:
            response = self._client().get(f"{self.url}/secretsmanager/get", params=params,
                                          headers={"X-Aws-Parameters-Secrets-Token": token})
        except Exception as exc:  # httpx2 transport errors
            raise SecretFetchError(f"the secrets agent at {self.url} is unreachable: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SecretFetchError(f"the secrets agent returned HTTP {response.status_code} for {secret_id}"
                                   + (" (its SSRF token was refused)" if response.status_code == 403 else ""))
        body = response.json()
        if not isinstance(body.get("SecretString"), str):
            raise SecretFetchError(f"secret {secret_id} has no SecretString (binary secrets are not supported)")
        return Fetched(_single_value(body["SecretString"], name), f"agent:{secret_id}", body.get("VersionId"))

    def paths(self, name: str) -> list[str]:
        return [self.token_file]


class AwsSdkFetcher(Fetcher):
    """AWS Secrets Manager through boto3 and the instance/pod role. boto3 is not a dependency of Inkcast;
    install it in your image (or use ``agent``/``dir`` mode, which need nothing extra)."""

    kind = "aws"

    def __init__(self, naming: SecretNaming, region: str | None = None):
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NotInstalledError("INKCAST_SECRETS_SOURCE=aws needs boto3, which is not installed in this image. "
                                    "Use INKCAST_SECRETS_SOURCE=agent (the Workload Credentials Provider) or dir "
                                    "(External Secrets Operator) instead.") from exc
        self.naming = naming
        self._client = boto3.client("secretsmanager", region_name=region or naming.region)

    def fetch(self, name: str, *, force: bool = False) -> Fetched | None:
        secret_id = self.naming.remote_id(name)
        try:
            r = self._client.get_secret_value(SecretId=secret_id)
        except Exception as exc:  # botocore errors; ResourceNotFound means "not here"
            if type(exc).__name__ == "ResourceNotFoundException":
                return None
            raise SecretFetchError(f"Secrets Manager refused {secret_id}: {type(exc).__name__}") from exc
        if "SecretString" not in r:
            raise SecretFetchError(f"secret {secret_id} has no SecretString (binary secrets are not supported)")
        return Fetched(_single_value(r["SecretString"], name), f"aws:{secret_id}", r.get("VersionId"))

    def describe(self, name: str) -> dict[str, Any] | None:
        r = self._client.describe_secret(SecretId=self.naming.remote_id(name))
        stages = r.get("VersionIdsToStages") or {}
        current = next((v for v, s in stages.items() if "AWSCURRENT" in s), None)
        return {"current_version": current, "rotation_enabled": bool(r.get("RotationEnabled")),
                "last_rotated": str(r.get("LastRotatedDate") or ""), "next_rotation": str(r.get("NextRotationDate") or "")}


class VaultFetcher(Fetcher):
    """OpenBao / HashiCorp Vault. Reserved: the seam exists so a future adapter (KV v2 over HTTP with
    Kubernetes auth) drops in here. Today, run the Vault Agent injector or External Secrets Operator and
    read the result with ``INKCAST_SECRETS_SOURCE=dir``."""

    kind = "vault"

    def __init__(self, naming: SecretNaming, address: str | None = None):
        raise NotInstalledError("INKCAST_SECRETS_SOURCE=vault is not available in this build. Use the Vault Agent "
                                "injector or External Secrets Operator with INKCAST_SECRETS_SOURCE=dir "
                                "(docs/reference/secrets.md).")

    def fetch(self, name: str, *, force: bool = False) -> Fetched | None:  # pragma: no cover - never constructed
        return None


class ChainFetcher(Fetcher):
    """Ask each source in turn; the first that has the secret wins."""

    kind = "chain"

    def __init__(self, *fetchers: Fetcher):
        self.fetchers = [f for f in fetchers if f is not None]
        self.kind = "+".join(f.kind for f in self.fetchers) or "chain"

    def fetch(self, name: str, *, force: bool = False) -> Fetched | None:
        for fetcher in self.fetchers:
            found = fetcher.fetch(name, force=force)
            if found is not None:
                return found
        return None

    def describe(self, name: str) -> dict[str, Any] | None:
        for fetcher in self.fetchers:
            meta = fetcher.describe(name)
            if meta:
                return meta
        return None

    def paths(self, name: str) -> list[str]:
        return [p for fetcher in self.fetchers for p in fetcher.paths(name)]


BACKENDS = {"aws": AwsSdkFetcher, "agent": AgentFetcher, "vault": VaultFetcher}


def fetcher_from_environ(environ: Mapping[str, str] | None = None, service: str = "inkcast") -> Fetcher:
    """The process's secret sources: ``env`` (always), ``INKCAST_SECRETS_DIR`` (when set), then the back-end
    named by ``INKCAST_SECRETS_SOURCE`` (``env`` by default; ``dir``, ``agent``, ``aws`` or ``vault``)."""
    env = environ if environ is not None else os.environ
    chain: list[Fetcher] = [EnvFetcher(env)]
    directory = env.get("INKCAST_SECRETS_DIR", "").strip()
    source = env.get("INKCAST_SECRETS_SOURCE", "env").strip().lower() or "env"
    if directory:
        chain.append(DirectoryFetcher(directory))
    if source == "dir" and not directory:
        raise SecretFetchError("INKCAST_SECRETS_SOURCE=dir needs INKCAST_SECRETS_DIR, e.g. /var/run/secrets/inkcast")
    if source in BACKENDS:
        naming = SecretNaming.from_environ(env, service=service)
        if source == "agent":
            chain.append(AgentFetcher(naming, url=env.get("INKCAST_AGENT_URL", "").strip() or "http://localhost:2773",
                                      token_file=env.get("INKCAST_AGENT_TOKEN_FILE", "").strip() or "/var/run/awssmatoken"))
        else:
            chain.append(BACKENDS[source](naming))
    elif source not in ("env", "dir"):
        raise SecretFetchError(f"INKCAST_SECRETS_SOURCE must be env, dir, agent, aws or vault (got {source!r})")
    return chain[0] if len(chain) == 1 else ChainFetcher(*chain)


__all__ = [
    "BACKENDS", "AgentFetcher", "AwsSdkFetcher", "ChainFetcher", "DirectoryFetcher", "EnvFetcher", "Fetched", "Fetcher",
    "NotInstalledError", "SecretFetchError", "VaultFetcher", "fetcher_from_environ",
]

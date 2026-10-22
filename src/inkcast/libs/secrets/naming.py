# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Secret naming convention and the "config files hold names, never values" rule.

Inkcast's secrets are addressed by their setting name (``INKCAST_SECRET_KEY``, ``OIDC_CLIENT_SECRET``...).
Local and Kubernetes sources find them by that name (an environment variable, a ``NAME_FILE`` path,
a file in ``INKCAST_SECRETS_DIR``). A remote secrets manager (AWS, OpenBao/Vault) needs a *remote id*
instead, and hand-typing one per environment drifts (``prod-inkcast-key``, ``inkcast/prod``,
``INKCAST_KEY_PRD``). So the remote id is rendered from a pattern:

    /{app}/{env}/{name}                      the default
    /inkcast/prod/secret-key                 INKCAST_SECRET_KEY   in prod
    /inkcast/prod/oidc-client-secret         OIDC_CLIENT_SECRET   in prod

Where the parts come from, highest first:

1. an explicit id: ``INKCAST_SECRET_ID_<NAME>`` (e.g. ``INKCAST_SECRET_ID_OIDC_CLIENT_SECRET=arn:aws:...``),
   for secrets whose name you don't choose;
2. the pattern (``INKCAST_SECRETS_NAME_PATTERN``, a preset name or your own) rendered with
   ``INKCAST_SECRETS_APP`` (default ``inkcast``), ``INKCAST_SECRETS_ENV`` (required; one of
   ``INKCAST_SECRETS_ALLOWED_ENVS``), the running service's full name and the secret's slug.

See docs/reference/secrets.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .redactor import is_sensitive_key

PRESETS: dict[str, str] = {
    "app-env-name": "/{app}/{env}/{name}",
    "app-env-service-name": "/{app}/{env}/{service}/{name}",
    "env-app-name": "/{env}/{app}/{name}",
    "env-app-service-name": "/{env}/{app}/{service}/{name}",
}
DEFAULT_PATTERN = PRESETS["app-env-name"]
DEFAULT_ENVIRONMENTS = ("dev", "staging", "prod")
TOKENS = frozenset({"app", "env", "service", "name", "region"})

# One path segment: the characters Secrets Manager and Vault accept, minus "/", so a value can't
# add or remove levels of the hierarchy.
_SEGMENT = re.compile(r"^[A-Za-z0-9_+=.@-]+$")
_NAME = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")
# A name ending in "-" plus 6 characters looks like the random suffix AWS adds to secret ARNs,
# and makes partial-ARN lookups and IAM wildcards ambiguous.
_LOOKS_LIKE_ARN_SUFFIX = re.compile(r"-[A-Za-z0-9]{6}$")

_URL_WITH_PASSWORD = re.compile(r"://[^/\s:@]*:[^/\s@]+@")


class NamingError(ValueError):
    """A secret name or pattern that can't be used; the message says how to fix it."""


class ConfigFileError(ValueError):
    """A config file that can't be used (unreadable, malformed, or holding a credential)."""


def secret_slug(setting: str) -> str:
    """``INKCAST_SECRET_KEY`` → ``secret-key``; ``OIDC_CLIENT_SECRET`` → ``oidc-client-secret``."""
    name = setting.strip()
    if name.upper().startswith("INKCAST_"):
        name = name[len("INKCAST_"):]
    return name.lower().replace("_", "-")


def render_secret_name(pattern: str, values: Mapping[str, str | None]) -> str:
    """Render ``pattern`` with ``values``. Raises ``NamingError`` in plain language on any problem."""
    pattern = PRESETS.get(pattern, pattern)
    used: set[str] = set()

    def fill(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in TOKENS:
            known = " ".join(f"{{{t}}}" for t in sorted(TOKENS))
            raise NamingError(f"unknown token {{{token}}} in secret name pattern {pattern!r} (known: {known})")
        value = values.get(token)
        if not value:
            raise NamingError(f"secret name pattern {pattern!r} needs {{{token}}}, which is not set")
        if not _SEGMENT.match(value):
            raise NamingError(f"{{{token}}}={value!r} may contain only letters, digits and _+=.@- (no '/')")
        used.add(token)
        return value

    name = re.sub(r"\{([^}]*)\}", fill, pattern)
    if "{" in name or "}" in name:
        raise NamingError(f"unbalanced braces in secret name pattern {pattern!r}")
    if "env" not in used:
        raise NamingError(f"secret name pattern {pattern!r} must contain {{env}}, or dev and prod would share a secret")
    if not _NAME.match(name) or "//" in name:
        raise NamingError(f"{name!r} is not a valid secret name (letters, digits, /_+=.@-, at most 512 characters, no '//')")
    return name


@dataclass(frozen=True)
class SecretNaming:
    """Turns a setting name into the id a remote secrets manager knows it by."""

    pattern: str = DEFAULT_PATTERN
    app: str = "inkcast"
    env: str | None = None
    service: str = "inkcast"
    region: str | None = None
    explicit: dict[str, str] = field(default_factory=dict)  # setting name → remote id
    allowed_envs: tuple[str, ...] = DEFAULT_ENVIRONMENTS

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], service: str = "inkcast") -> "SecretNaming":
        explicit = {k[len("INKCAST_SECRET_ID_"):]: v.strip() for k, v in environ.items()
                    if k.startswith("INKCAST_SECRET_ID_") and v.strip()}
        allowed = tuple(s.strip() for s in environ.get("INKCAST_SECRETS_ALLOWED_ENVS", "").split(",") if s.strip())
        return cls(
            pattern=environ.get("INKCAST_SECRETS_NAME_PATTERN", "").strip() or DEFAULT_PATTERN,
            app=environ.get("INKCAST_SECRETS_APP", "").strip() or "inkcast",
            env=environ.get("INKCAST_SECRETS_ENV", "").strip() or None,
            service=service,
            region=environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or None,
            explicit=explicit,
            allowed_envs=allowed or DEFAULT_ENVIRONMENTS,
        )

    def remote_id(self, setting: str) -> str:
        """The remote id for ``setting``: an explicit ``INKCAST_SECRET_ID_<NAME>``, else the rendered pattern."""
        if setting in self.explicit:
            return self.explicit[setting]
        if not self.env:
            raise NamingError(f"no remote id for {setting}: set INKCAST_SECRET_ID_{setting}, or INKCAST_SECRETS_ENV for "
                              "the naming convention (docs/reference/secrets.md)")
        if self.env not in self.allowed_envs:
            raise NamingError(f"INKCAST_SECRETS_ENV={self.env!r} is not one of {', '.join(self.allowed_envs)}")
        return render_secret_name(self.pattern, {"app": self.app, "env": self.env, "service": self.service,
                                                 "name": secret_slug(setting), "region": self.region})

    def warnings(self, settings: tuple[str, ...]) -> list[str]:
        out = []
        for setting in settings:
            try:
                name = self.remote_id(setting)
            except NamingError:
                continue
            if _LOOKS_LIKE_ARN_SUFFIX.search(name):
                out.append(f"{name} ends in '-' plus 6 characters, like the suffix AWS adds to secret ARNs; "
                           "prefer a name without it")
        return out


# ---- config files: names only ------------------------------------------------------------


def assert_no_credentials(node: Any, path: str, at: str = "") -> None:
    """A config file says *where* secrets are, never what they are. Raises ``ConfigFileError``."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        here = f"{at}.{key}" if at else str(key)
        if is_sensitive_key(key):  # INKCAST_SECRET_KEY, no; INKCAST_SECRET_KEY_FILE (a pointer), yes
            raise ConfigFileError(f"{path}: {here!r} looks like a credential; config files hold secret names only")
        if isinstance(value, str) and _URL_WITH_PASSWORD.search(value):
            raise ConfigFileError(f"{path}: {here!r} contains a URL with a password; config files hold secret names only")
        assert_no_credentials(value, path, here)


def _parse_env_file(text: str, path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ConfigFileError(f"{path}:{n}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        out[key] = value
    return out


def load_config_file(path: str) -> dict[str, str]:
    """Read ``INKCAST_CONFIG_FILE``: ``.env`` (KEY=VALUE), ``.yaml``/``.yml`` or ``.json`` (a flat mapping of the
    same names). Returns environment-style settings; real environment variables win over them.
    Credentials are refused: the file may hold ``INKCAST_SECRET_KEY_FILE``, never ``INKCAST_SECRET_KEY``."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigFileError(f"{path}: can't be read: {exc}") from exc
    suffix = p.suffix.lower()
    if suffix == ".env" or p.name.endswith("config.env"):
        data: Any = _parse_env_file(text, path)
    elif suffix in (".yaml", ".yml"):
        import yaml

        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:  # its text quotes the offending line, which may hold a credential: position only
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise ConfigFileError(f"{path}: not valid YAML{where}") from None
    elif suffix == ".json":
        try:
            data = json.loads(text or "{}")
        except ValueError as exc:
            raise ConfigFileError(f"{path}: not valid JSON: {exc}") from exc
    else:
        raise ConfigFileError(f"{path}: unsupported config file type (use .env, .yaml, .yml or .json)")
    if not isinstance(data, dict):
        raise ConfigFileError(f"{path}: must be a mapping of setting names to values")
    assert_no_credentials(data, path)
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            raise ConfigFileError(f"{path}: {key!r} must be a scalar (nested settings are not supported)")
        if value is None:
            continue
        out[str(key)] = str(value).strip() if not isinstance(value, bool) else str(value).lower()
    return out


__all__ = [
    "DEFAULT_ENVIRONMENTS", "DEFAULT_PATTERN", "PRESETS", "TOKENS", "ConfigFileError", "NamingError", "SecretNaming",
    "assert_no_credentials", "load_config_file", "render_secret_name", "secret_slug",
]

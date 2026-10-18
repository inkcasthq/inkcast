# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""How a secret is *described* without being shown: a length and an HMAC fingerprint.

    secret_key      -> ******** (48 chars, fp=1a2b3c4d5e6f)   nothing of the value
    host, port, url -> shown in full                           not secret, needed to debug
    anything else   -> partly masked: no*****pp               e.g. a username

The fingerprint is an HMAC under a per-process random key (``SecretsProvider`` makes one at start):
two lines with the same fp saw the same value, a changed fp means the value changed, and the value
cannot be recovered or guessed from it. It is safe in logs and on the metrics port.
"""

from __future__ import annotations

import hmac
import hashlib
from typing import Any, Iterable

from .redactor import is_sensitive_key

SHOWN_IN_FULL = frozenset({"host", "port", "url", "issuer", "endpoint", "region", "name", "source", "version"})


def fingerprint(value: str, key: bytes, length: int = 12) -> str:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:length]


def fully_mask(value: str, key: bytes) -> str:
    return f"******** ({len(value)} chars, fp={fingerprint(value, key)})"


def partly_mask(value: str) -> str:
    s = str(value)
    if len(s) <= 2:
        return "*" * len(s)
    if len(s) <= 6:
        return s[0] + "*" * (len(s) - 1)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def mask_value(name: str, value: Any, key: bytes) -> str:
    if value is None:
        return "None"
    if hasattr(value, "reveal"):  # a SecretValue
        return fully_mask(value.reveal(), key)
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return "{" + ", ".join(map(str, value)) + "}"
    if is_sensitive_key(name):
        return fully_mask(str(value), key)
    if not isinstance(value, str):
        return str(value)  # numbers, booleans
    if name.lower() in SHOWN_IN_FULL:
        return value
    return partly_mask(value)


def describe_keys(mapping: dict[str, Any], key: bytes) -> list[str]:
    """``["username=no*****pp", "password=******** (40 chars, fp=1a2b3c4d5e6f)", "host=postgres"]``.

    Entries are strings rather than a mapping keyed by the secret's own key names, because a
    structured redactor blanks any field *named* like a credential, which would hide the
    description too.
    """
    return [f"{k}={mask_value(k, v, key)}" for k, v in mapping.items()]


def describe_names(names: Iterable[str], values: dict[str, Any], key: bytes) -> list[str]:
    return describe_keys({n: values.get(n) for n in names}, key)


__all__ = ["SHOWN_IN_FULL", "describe_keys", "describe_names", "fingerprint", "fully_mask", "mask_value", "partly_mask"]

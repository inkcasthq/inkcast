# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``SecretValue`` and the small primitives for minting and storing bearer secrets.

A ``SecretValue`` wraps a secret so the accidental paths out of the process print nothing useful:
``repr``, ``str``, f-strings, ``logging`` ``%s``/``%r`` and ``json.dumps`` (which refuses it). The
real value is only available through an explicit ``reveal()`` call at the point of use, which also
makes every use easy to find with ``grep reveal(`` during code review.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def token_hash(value: str) -> str:
    """How bearer secrets (tokens, codes, tickets) are stored: only their SHA-256 hex digest."""
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


class SecretValue:
    """A secret that prints as ``***``. Call ``reveal()`` at the one place it is really needed.

    Comparing with ``==`` works against another ``SecretValue`` or a plain string, in constant time,
    so ``if config.password == supplied`` is safe to write. Nothing else gives the value away:
    ``len()``, ``in``, slicing and ``json.dumps`` all fail rather than leak.
    """

    __slots__ = ("_value",)

    def __init__(self, value: "str | SecretValue"):
        if isinstance(value, SecretValue):
            value = value._value
        if not isinstance(value, str):
            raise TypeError(f"SecretValue wraps a str, not {type(value).__name__}")
        self._value = value

    def reveal(self) -> str:
        """The real value. Use it at the point of use only; never store or log the result."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "SecretValue('***')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "SecretValue('***')"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            other = other._value
        if not isinstance(other, str):
            return NotImplemented
        return secrets.compare_digest(self._value.encode(), other.encode())

    def __hash__(self) -> int:
        return hash(token_hash(self._value))

    def __reduce__(self):  # no pickling: a secret must not travel to another process by accident
        raise TypeError("SecretValue can't be pickled; reveal() it at the point of use instead")

    def __copy__(self) -> "SecretValue":
        return self

    def __deepcopy__(self, memo: dict) -> "SecretValue":
        return self


def as_str(value: "str | SecretValue") -> str:
    """Accept a plain string or a ``SecretValue`` where a library needs the raw text (e.g. a key)."""
    return value.reveal() if isinstance(value, SecretValue) else value


class Sealer:
    """Encrypts platform credentials at rest with a key derived from INKCAST_SECRET_KEY."""

    def __init__(self, secret_key: "str | SecretValue"):
        key = hashlib.sha256(("inkcast-credentials:" + as_str(secret_key)).encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def unseal(self, sealed: str) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(sealed.encode()))
        except InvalidToken as exc:
            raise RuntimeError(
                "Stored credentials can't be decrypted; INKCAST_SECRET_KEY changed. Reconnect the platform."
            ) from exc


__all__ = ["SecretValue", "Sealer", "as_str", "new_secret", "token_hash"]

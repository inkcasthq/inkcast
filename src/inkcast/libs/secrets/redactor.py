# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Value-based redaction: every secret value this process has loaded is registered here, and any
occurrence of it in text that leaves the process (a log line, a traceback, an error body, the
masked describe-keys output) is replaced with ``[REDACTED]``, whatever field it ended up in.

This is the "last mile" layer. The pattern layer in ``libs.observability.redact`` catches things that
*look* like tokens; this layer catches the exact values we know, even inside a URL or a library's
error message. ``libs.observability`` applies both to every log line.

One process-wide instance, ``REDACTOR``, is what ``SecretsProvider`` and ``Config`` register into and
what the log formatters read. Tests may build their own ``Redactor()``.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any
from urllib.parse import quote

MIN_SECRET_LENGTH = 8  # shorter strings would mask ordinary words
MAX_REMEMBERED = 200  # old values stay registered after rotation: they may still show up in logs

SENSITIVE_KEYS = frozenset({
    "password", "passwd", "pwd", "pass", "secret", "secretstring", "secret_string", "secret_key",
    "token", "authtoken", "auth_token", "accesstoken", "access_token", "refreshtoken", "refresh_token",
    "authorization", "cookie", "apikey", "api_key", "api-key", "privatekey", "private_key",
    "clientsecret", "client_secret", "secretaccesskey", "aws_secret_access_key", "sessiontoken",
    "x-aws-parameters-secrets-token", "x-vault-token", "code_verifier", "ticket",
})

# scheme://user:password@host  ->  scheme://user:[REDACTED]@host (the user may be empty: redis://:password@host)
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]*:)[^@\s/]+@", re.I)


# Names that mention a secret but hold a pointer or a setting, not the value: INKCAST_SECRET_KEY_FILE,
# INKCAST_SECRETS_DIR, DB_SECRET_ID, token_hash, SECRETS_NAME_PATTERN...
_BENIGN_SUFFIXES = ("_file", "_path", "_dir", "_id", "_arn", "_name", "_names", "_pattern", "_env", "_envs", "_ttl",
                    "_seconds", "_mode", "_source", "_url", "_hash", "_prefix", "_count", "_length")
_SENSITIVE_WORDS = ("password", "passwd", "secret", "token", "api_key", "apikey", "api-key", "private_key", "credential")


def is_sensitive_key(key: Any) -> bool:
    """Field names that hold credentials. Used by ``scrub_value``, the masked describe-keys output and the
    posture checks. A name is sensitive when it is a known credential field or contains a credential
    word, unless its suffix says it holds a pointer or a setting (``_FILE``, ``_ID``, ``_PATTERN``...)."""
    k = str(key).lower()
    if k in SENSITIVE_KEYS:
        return True
    if k.endswith(_BENIGN_SUFFIXES):
        return False
    return any(word in k for word in _SENSITIVE_WORDS) or k.endswith("_key")


class Redactor:
    def __init__(self) -> None:
        self._values: dict[str, None] = {}  # insertion-ordered set
        self._regex: re.Pattern[str] | None = None
        self._lock = threading.Lock()

    # ---- registration --------------------------------------------------------

    def register(self, value: Any) -> None:
        """Remember a secret value (and the ways it is commonly encoded) so it is masked everywhere."""
        if not isinstance(value, str) or len(value) < MIN_SECRET_LENGTH:
            return
        variants = {value, json.dumps(value)[1:-1], quote(value, safe="")}
        with self._lock:
            for v in variants:
                self._values.pop(v, None)
                self._values[v] = None
            while len(self._values) > MAX_REMEMBERED:
                del self._values[next(iter(self._values))]
            alternatives = sorted(self._values, key=len, reverse=True)
            self._regex = re.compile("|".join(re.escape(v) for v in alternatives))

    def register_mapping(self, mapping: Any, raw: str | None = None) -> None:
        """Register ``raw`` (a whole secret string) and every credential-looking leaf of a parsed secret."""
        if raw is not None:
            self.register(raw)
        stack = [mapping]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str) and is_sensitive_key(key):
                        self.register(value)
                    elif isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)

    @property
    def size(self) -> int:
        return len(self._values)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._regex = None

    # ---- scrubbing -------------------------------------------------------------

    def contains(self, text: Any) -> bool:
        regex = self._regex
        return isinstance(text, str) and regex is not None and regex.search(text) is not None

    def scrub(self, text: str) -> str:
        """Replace registered values and URL passwords in ``text``."""
        if not isinstance(text, str):
            return text
        regex = self._regex
        if regex is not None:
            text = regex.sub("[REDACTED]", text)
        return _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)

    def scrub_value(self, value: Any, depth: int = 0) -> Any:
        """Deep-copy ``value`` for output: sensitive keys blanked, strings scrubbed, errors reduced to safe fields."""
        if depth > 8:
            return "[Truncated]"
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, BaseException):
            return {"type": type(value).__name__, "message": self.scrub(str(value))}
        if isinstance(value, dict):
            return {str(k): ("[REDACTED]" if is_sensitive_key(k) and v not in (None, "") else self.scrub_value(v, depth + 1))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.scrub_value(v, depth + 1) for v in value]
        if hasattr(value, "reveal"):  # a SecretValue: never its content
            return "[REDACTED]"
        return value


REDACTOR = Redactor()

__all__ = ["MAX_REMEMBERED", "MIN_SECRET_LENGTH", "REDACTOR", "Redactor", "SENSITIVE_KEYS", "is_sensitive_key"]

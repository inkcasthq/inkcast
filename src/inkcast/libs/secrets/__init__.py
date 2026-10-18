# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Secret handling shared by every service.

=================  ======================================================================
``value``          ``SecretValue`` (prints as ``***``, ``reveal()`` at the point of use),
                   ``Sealer`` (credentials at rest), ``token_hash``/``new_secret`` (bearer secrets)
``redactor``       ``REDACTOR``: every loaded secret value is registered here and masked in every
                   log line, traceback and error body, whatever field it ended up in
``mask``           describe a secret without showing it: length + HMAC fingerprint
``provider``       ``SecretsProvider``: TTL with jitter, single-flight, stale-on-error
=================  ======================================================================

Import from this package (``from inkcast.libs.secrets import SecretValue``); the module layout
below it may change.
"""

from __future__ import annotations

from .mask import describe_keys, fingerprint, fully_mask, mask_value, partly_mask
from .provider import SecretsProvider
from .redactor import REDACTOR, Redactor, is_sensitive_key
from .value import Sealer, SecretValue, as_str, new_secret, token_hash

__all__ = [
    "REDACTOR", "Redactor", "Sealer", "SecretValue", "SecretsProvider", "as_str", "describe_keys", "fingerprint",
    "fully_mask", "is_sensitive_key", "mask_value", "new_secret", "partly_mask", "token_hash",
]

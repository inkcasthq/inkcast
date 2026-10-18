# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Secret handling shared by every service.

=================  ======================================================================
``value``          ``SecretValue`` (prints as ``***``, ``reveal()`` at the point of use),
                   ``Sealer`` (credentials at rest), ``token_hash``/``new_secret`` (bearer secrets)
``redactor``       ``REDACTOR``: every loaded secret value is registered here and masked in every
                   log line, traceback and error body, whatever field it ended up in
``mask``           describe a secret without showing it: length + HMAC fingerprint
``fetchers``       where secrets come from: env/``NAME_FILE``, a mounted directory, the AWS agent,
                   boto3 (optional), OpenBao/Vault (reserved); ``fetcher_from_environ`` picks
``naming``         the naming convention for remote ids, and "config files hold names only"
``provider``       ``SecretsProvider``: TTL with jitter, single-flight, stale-on-error, refresh(),
                   on_change(), fingerprints, status()
=================  ======================================================================

Import from this package (``from inkcast.libs.secrets import SecretValue``); the module layout
below it may change.
"""

from __future__ import annotations

from .mask import describe_keys, fingerprint, fully_mask, mask_value, partly_mask
from .fetchers import (
    AgentFetcher, AwsSdkFetcher, ChainFetcher, DirectoryFetcher, EnvFetcher, Fetched, Fetcher, NotInstalledError,
    SecretFetchError, VaultFetcher, fetcher_from_environ,
)
from .naming import ConfigFileError, NamingError, SecretNaming, load_config_file, render_secret_name, secret_slug
from .provider import SecretEvent, SecretNotFound, SecretsProvider
from .redactor import REDACTOR, Redactor, is_sensitive_key
from .value import Sealer, SecretValue, as_str, new_secret, token_hash

__all__ = [
    "REDACTOR", "AgentFetcher", "AwsSdkFetcher", "ChainFetcher", "ConfigFileError", "DirectoryFetcher", "EnvFetcher",
    "Fetched", "Fetcher", "NamingError", "NotInstalledError", "Redactor", "Sealer", "SecretEvent", "SecretFetchError",
    "SecretNaming", "SecretNotFound", "SecretValue", "SecretsProvider", "VaultFetcher", "as_str", "describe_keys",
    "fetcher_from_environ", "fingerprint", "fully_mask", "is_sensitive_key", "load_config_file", "mask_value", "new_secret",
    "partly_mask", "render_secret_name", "secret_slug", "token_hash",
]

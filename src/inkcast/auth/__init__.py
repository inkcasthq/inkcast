# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Moved to ``inkcast.services.identity`` (provider, login) and ``inkcast.libs.http.pages``."""

from .._compat import alias

alias(f"{__name__}.provider", "inkcast.services.identity.provider")
alias(f"{__name__}.identity", "inkcast.services.identity.login")
alias(f"{__name__}.pages", "inkcast.libs.http.pages")

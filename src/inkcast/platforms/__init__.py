# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Moved to ``inkcast.libs.platforms``. Plug-ins may keep importing ``inkcast.platforms``."""

from .._compat import alias

for _name in ("base", "devto", "medium"):
    alias(f"{__name__}.{_name}", f"inkcast.libs.platforms.{_name}")
alias(__name__, "inkcast.libs.platforms")

# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Keep v0.1 import paths working after the move to libs/ and services/ (same module objects)."""

from __future__ import annotations

import importlib
import sys


def alias(old: str, new: str) -> None:
    sys.modules[old] = importlib.import_module(new)

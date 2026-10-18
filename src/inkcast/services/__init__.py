# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Backend services. Each package exposes ``SERVICE: ServiceSpec``; the launcher runs any of them by
CamelCase name (INKCAST_APP_NAME). Services import only from ``inkcast.libs``, never each other."""

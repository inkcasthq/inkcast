# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Shared libraries: reusable building blocks every service is made of.

Rule (enforced by tests/test_architecture.py): nothing under ``libs`` imports ``inkcast.services``,
and no service imports another service. Services talk through contracts (libs.contracts),
the store, and HTTP.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of the role-organization guard (#71, PR #99): each test failed on the PR's head and names
the behaviour it pins. OpenFGA references are to its v1.22.0 sources, the version fga CLI 0.8.2 (CI's authz job)
embeds."""

from __future__ import annotations

import asyncio
import pathlib
import unittest

import yaml

from inkcast.libs.authz import LocalEngine, Ref, Tuple
from inkcast.libs.db import Store

MODEL_TESTS = pathlib.Path(__file__).resolve().parents[1] / "authz" / "model.fga.yaml"
LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


class UsersetSubjectThroughMemberFromOrg(unittest.TestCase):
    """OpenFGA's ``LocalChecker.ResolveCheck`` (internal/graph/check.go) answers allowed for a self-defining tuple
    (``tuple.IsSelfDefining``: ``organization:acme#member`` member ``organization:acme``) before reading anything,
    and it runs on every dispatch, including the one ``member from org`` makes from a role. So an organization's
    members userset assigned to the organization's own role is a ``member_assignee`` of it, and an editor where the
    role is granted. The local engine answered false for both, so the two engines disagreed on the new guard."""

    def setUp(self):
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        self.engine = LocalEngine(store)
        doc = yaml.safe_load(MODEL_TESTS.read_text())
        run(self.engine.write([Tuple(t["user"], t["relation"], t["object"]) for t in doc["tuples"]]))
        run(self.engine.write([Tuple("organization:acme#member", "assignee", "role:acme-copy-editor")]))

    def check(self, user: str, relation: str, obj: str) -> bool:
        return run(self.engine.check(user, relation, Ref.parse(obj)))

    def test_a_userset_is_in_itself(self):
        self.assertTrue(self.check("organization:acme#member", "member", "organization:acme"))
        self.assertFalse(self.check("organization:acme#member", "member", "organization:globex"))
        self.assertFalse(self.check("organization:acme#member", "admin", "organization:acme"))

    def test_the_members_userset_of_the_roles_own_organization_is_a_member_assignee(self):
        self.assertTrue(self.check("organization:acme#member", "member_assignee", "role:acme-copy-editor"))
        self.assertTrue(self.check("organization:acme#member", "editor", "workspace:acme-blog"))
        self.assertFalse(self.check("organization:acme#member", "manager", "workspace:acme-blog"))
        # The guard still closes case 1 for the userset: Globex's role isn't Acme's.
        self.assertFalse(self.check("organization:acme#member", "member_assignee", "role:globex-ghost"))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of #290 (PR #408): a re-run that a result asks for runs, measured against the running stack. A re-run from
the CLI or the action sends the same body, so the same ``Idempotency-Key``."""

import asyncio
import unittest

import httpx2

from test_e2e import Harness
from test_git_publishing import story


class ReRunReview(Harness):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.config.public_url
        h = {"Authorization": f"Bearer {cls('test_a_failed_result_is_tried_again_by_the_rerun').access_token()}"}

        async def setup():
            async with httpx2.AsyncClient(timeout=30) as c:
                link = (await c.post(f"{cls.base}/api/v1/platforms/devto/connect-link", headers=h)).json()
                await c.post(link["url"], data={"api_key": "devto-key"})
                r = await c.post(f"{cls.base}/api/v1/tokens", headers=h, json={"name": "blog CI", "scopes": ["stories:sync"]})
                return r.json()
        cls.pat = asyncio.run(setup())

    def post(self, body: dict, key: str) -> httpx2.Response:
        async def run():
            async with httpx2.AsyncClient(timeout=30) as c:
                return await c.post(f"{self.base}/api/v1/stories:sync", json=body,
                                    headers={"Authorization": f"Bearer {self.pat['token']}", "Idempotency-Key": key})
        return asyncio.run(run())

    def test_a_failed_result_is_tried_again_by_the_rerun(self):
        """``failed``: "the next sync tries again" (docs/git-publishing.md), and DEV's refusal of the key says to
        reconnect it. The person does, and re-runs the job: the re-run publishes, once."""
        source, before = f"github.com/acme/blog-{self.id()}", len(self.fakes.devto_articles)
        body = {"source": source, "files": [{"path": "posts/key.md", "markdown": story("Key refused", platforms="[devto]")}]}
        self.fakes.devto_fail_next = 401                                            # the key was revoked
        [first] = self.post(body, key="refused").json()["results"]
        self.assertEqual(first["action"], "failed", first)
        rerun = self.post(body, key="refused")                                      # reconnected, the job re-run
        self.assertIsNone(rerun.headers.get("idempotent-replayed"), "the failed answer was replayed")
        [second] = rerun.json()["results"]
        self.assertEqual((second["action"], second["status"]), ("create", "published"), second)
        again = self.post(body, key="refused")                                      # nothing failed: replayed
        self.assertEqual((again.headers.get("idempotent-replayed"), len(self.fakes.devto_articles)), ("true", before + 1))

    def test_the_api_reference_says_when_a_reused_key_is_refused(self):
        """A key freed for a re-run is free: the same key with a different body then runs, where one kept for replay
        gets 422 ``idempotency_key_reused``. The API reference said 422 always."""
        source = f"github.com/acme/blog-{self.id()}"
        skipped = {"source": source, "files": [{"path": "posts/s.md", "markdown": story("Not connected", platforms="[medium]")}]}
        other = {"source": source, "files": [{"path": "posts/o.md", "markdown": story("Other file", platforms="[medium]")}]}
        self.assertEqual(self.post(skipped, key="freed").json()["results"][0]["action"], "skipped")
        self.assertEqual(self.post(other, key="freed").status_code, 200)
        kept = {"source": source, "files": [{"path": "posts/k.md", "markdown": story("Kept answer", platforms="[devto]")}]}
        self.assertEqual(self.post(kept, key="kept").json()["results"][0]["action"], "create")
        self.assertEqual(self.post(other, key="kept").json()["error"], "idempotency_key_reused")
        described = self.call_openapi()["paths"]["/api/v1/stories:sync"]["post"]["description"]
        self.assertIn("the same key with a different body gets 422 `idempotency_key_reused` while the first "
                      "response is kept", described)

    def call_openapi(self) -> dict:
        async def run():
            async with httpx2.AsyncClient(timeout=30) as c:
                return (await c.get(f"{self.base}/api/openapi.json")).json()
        return asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

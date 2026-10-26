# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #192 (issue #173): a 3xx answer to a platform request.

Each test failed on the PR's head before the fix committed with it."""

import asyncio
import json
import unittest

import httpx2

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import Publisher
from inkcast.libs.platforms import Account, PlatformError
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.services.publishing.sync import GitSync


def see_other_api():
    """A Forem API that creates the article and answers the POST with ``303 See Other`` pointing at it (the
    post/redirect/get pattern), and lists the account's articles on ``GET /articles/me/all``."""
    articles: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and request.url.path == "/api/articles":
            article = json.loads(request.content)["article"]
            articles.append({"id": len(articles) + 1, "title": article["title"], "published": True,
                             "url": f"https://dev.to/on-call/{len(articles) + 1}"})
            return httpx2.Response(303, headers={"Location": f"https://dev.to/api/articles/{len(articles)}"})
        if request.method == "GET" and request.url.path == "/api/articles/me/all":
            return httpx2.Response(200, json=articles)
        return httpx2.Response(404)

    return DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(handler))), articles


class SeeOtherAfterACreate(unittest.TestCase):
    """RFC 9110 §15.4.4: a 303 to a POST is the server's *indirect* answer, its result at the Location: the request
    reached it and was acted on. The PR reads every 3xx as "never reached the API, nothing was published": a
    definite failure, which Git sync creates again on the next sync, and the person is told to fix the API URL."""

    def test_a_303_on_a_publish_is_uncertain_not_nothing_was_published(self):
        devto, articles = see_other_api()
        prepared = devto.prepare({"title": "Hello"}, "Body.")
        with self.assertRaises(PlatformError) as caught:
            asyncio.run(devto.publish({"api_key": "k"}, Account(id="1", name="on-call"), prepared, None))
        self.assertEqual(len(articles), 1, "the platform did create it")
        self.assertEqual((caught.exception.uncertain, caught.exception.retryable), (True, False))

    def test_git_sync_does_not_create_the_post_again_after_a_303(self):
        devto, articles = see_other_api()
        store = Store(":memory:", "k" * 40)
        self.addCleanup(store.close)
        user = store.upsert_user("local:on-call", "on-call", None, None)["id"]
        store.save_credentials(user, devto.id, {"api_key": "k"}, {"id": "1", "name": "on-call"})
        sync = GitSync(store, {devto.id: devto}, Publisher(store, {devto.id: devto}))
        req = SyncRequest(source="github.com/acme/blog", files=[
            SyncFile(path="posts/hello.md", markdown="---\ntitle: Hello\nplatforms: [devto]\n---\n\nBody.\n")])
        asyncio.run(sync.sync(user, req))
        asyncio.run(sync.sync(user, req))   # the next commit's sync
        self.assertEqual(len(articles), 1, "the article was created twice")
        self.assertEqual(store.get_entry(user, req.source, "posts/hello.md", devto.id)["status"], "published")


if __name__ == "__main__":
    unittest.main()

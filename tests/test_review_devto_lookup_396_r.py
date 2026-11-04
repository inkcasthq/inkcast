# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Adversarial review of PR #420 (issue #396): DEV's ``find_existing`` matches by title.

``None`` from ``find_existing`` is "missing": recovery sends the post again. So every doubt must be "can't tell" (a
result naming neither URL nor id), never ``None``. The recorded answers follow forem/forem@d4190ce:
- ``GET /articles/me/all`` orders by ``published_at DESC, created_at DESC`` (app/controllers/concerns/api/
  articles_controller.rb ``me``): on PostgreSQL a NULL sorts first in descending order, so every draft comes before
  the article a published send made, and ``per_page`` cuts the list;
- ``Article#normalize_title`` (app/models/article.rb) replaces characters outside ``TITLE_CHARACTERS_ALLOWED`` (``→``,
  U+2192, among them) with a space, collapses whitespace and strips: the title DEV lists is not always the one sent;
- the ``me`` view emits ``published`` and ``body_markdown`` as stored.
"""

import asyncio
import json
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.contracts.models import SyncFile, SyncRequest
from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms.devto import DevtoPlatform
from inkcast.services.publishing.sync import GitSync

from test_devto_lookup_396 import article


class DEV:
    """A fake of DEV's API: ``listing`` is what ``GET /articles/me/all`` answers (page 1, ``per_page`` cut);
    ``POST /articles`` answers ``post_status`` and, when it is a 2xx or 5xx, the article arrives."""

    def __init__(self, listing: list[dict], post_status: int = 201):
        self.listing, self.post_status, self.posts, self.puts = listing, post_status, [], []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/api/articles/me/all":
            return httpx2.Response(200, json=self.listing[:int(request.url.params["per_page"])])
        body = json.loads(request.content)["article"]
        if request.method == "POST":
            self.posts.append(body)
            made = article(2000 + len(self.posts), body["title"], body["body_markdown"], published=body["published"])
            self.listing.insert(0, made)
            return httpx2.Response(self.post_status, json=made)
        self.puts.append((request.url.path, body["body_markdown"]))
        return httpx2.Response(200, json={**article(2001, body["title"], body["body_markdown"])})


def recovered(listing: list[dict], fields: dict, body: str = "Body.") -> tuple[dict, DEV]:
    """A DEV post whose send was interrupted (left 'publishing' past STUCK_AFTER), recovered by the scheduler's pass
    against ``listing``, then the pass after it: ``(the post, the fake)``."""
    store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
    dev = DEV(listing)
    devto = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(dev)))
    user = store.upsert_user("local:on-call", "on-call", None, None)["id"]
    store.save_credentials(user, devto.id, {"api_key": "k"}, {"id": "42", "name": "On Call"})
    prepared = devto.prepare(fields, body)
    post_id = store.create_post(group_id="g", user_id=user, platform=devto.id, title=prepared.title, status="publishing",
                                run_at=time.time() - STUCK_AFTER - 2, payload=prepared.to_dict())
    store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
    publisher = Publisher(store, {devto.id: devto})
    asyncio.run(publisher.run_due(now=time.time()))
    asyncio.run(publisher.run_due(now=time.time() + 1))
    post = store.get_post(post_id)
    store.close()
    return post, dev


def drafts(n: int) -> list[dict]:
    return [article(5000 + i, f"Draft {i}", f"Draft body {i}.", published=False) for i in range(n)]


class FindingTheSyncAsksAboutWhatWasSent(unittest.TestCase):
    """Git sync's check of an unconfirmed post (#213, #226) asked DEV about the file as it is now. Once the person
    edits the file, the article the uncertain send made has other content, and the check could no longer find it: the
    post stayed unconfirmed for a person to decide, where main recorded it and sent the edit as an update."""

    def test_an_edited_file_still_finds_the_article_its_uncertain_send_made(self):
        store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(store.close)
        dev = DEV([], post_status=502)
        devto = DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(dev)))
        user = store.upsert_user("local:on-call", "on-call", None, None)["id"]
        store.save_credentials(user, devto.id, {"api_key": "k"}, {"id": "42", "name": "On Call"})
        publisher = Publisher(store, {devto.id: devto})
        git = GitSync(store, {devto.id: devto}, publisher)

        def sync(body: str):
            md = f"---\ntitle: Hello\nplatforms: [devto]\n---\n\n{body}\n"
            req = SyncRequest(source="github.com/acme/blog", files=[SyncFile(path="posts/hello.md", markdown=md)])
            (result,) = asyncio.run(git.sync(user, req)).results
            return result

        first = sync("Body.")
        self.assertEqual(first.status, "unconfirmed")                   # a 502 on the POST: it may have arrived (it did)
        edited = sync("Edited.")
        post = store.get_post(first.post_id)
        self.assertEqual((post["status"], post["remote_id"]), ("published", "2001"),
                         "the article the send made was not found: the post is left to a person")
        self.assertEqual((edited.action, dev.puts), ("update", [("/api/articles/2001", "Edited.\n")]))
        self.assertEqual(dev.posts[1:], [], "posted again")



if __name__ == "__main__":
    unittest.main()

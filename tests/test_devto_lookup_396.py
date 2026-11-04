# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Issue #396: DEV's recovery lookup returns only an article this send could have created.

``find_existing`` took the first article in ``/articles/me/all`` whose title matched, so an older article or draft with
the same title, or a first copy a person was told to remove, was recorded as the post's copy: the row named the wrong
article, and the send's own late success was refused over it (#390's fence). Now an article is this send's only when it
agrees with what was sent on everything DEV lets Inkcast compare: the title, the canonical URL when the send set one,
and the body when the answer carries it, which tells an older article with the same title apart. Exactly one such
article is the post; nothing with the title or
the canonical URL is "missing" (the post is sent again); anything else is "can't tell", a result naming neither URL nor
id with the reason in ``notes``, which the pipeline leaves to a person (``Publisher.confirm``).

The recorded answers have the shape DEV gives ``GET /articles/me/all`` (forem/forem@d4190ce,
app/views/api/v1/articles/me.json.jbuilder): ``published`` and ``body_markdown`` are there, though the documented
``ArticleIndex`` (swagger/v1/api_v1.json@89422b0) lists neither, and ``created_at`` is not, though documented;
``canonical_url`` is the article's own URL when none was set (``processed_canonical_url``). ``...AnOlderArticle...``
failed on main; the rest pin what holds."""

import asyncio
import logging
import tempfile
import time
import unittest

import httpx2

from inkcast.libs.db import Store
from inkcast.libs.pipeline import STUCK_AFTER, Publisher
from inkcast.libs.platforms import Account
from inkcast.libs.platforms.devto import DevtoPlatform

ME_ALL = "https://dev.to/api/articles/me/all"


def article(id: int, title: str, body: str, *, canonical: str | None = None, published: bool = True) -> dict:
    """One item of DEV's answer, every field the ``me`` view emits; the body as DEV stores it."""
    slug = f"{title.lower().replace(' ', '-')}-{id}"
    url = f"https://dev.to/on-call/{slug}"
    when = "2026-10-30T09:00:00Z"
    return {"type_of": "article", "id": id, "title": title, "description": body[:40], "published": published,
            "published_at": when if published else None, "slug": slug, "path": f"/on-call/{slug}", "url": url,
            "comments_count": 0, "public_reactions_count": 0, "page_views_count": 0,
            "published_timestamp": when if published else "", "body_markdown": body, "positive_reactions_count": 0,
            "cover_image": None, "tag_list": [], "canonical_url": canonical or url, "reading_time_minutes": 1,
            "user": {"name": "On Call", "username": "on-call", "twitter_username": None, "github_username": None,
                     "user_id": 42, "website_url": None, "profile_image": "", "profile_image_90": ""}}


def documented(id: int, title: str, *, canonical: str | None = None) -> dict:
    """The same item with the documented ``ArticleIndex`` fields only: no body, no ``published``."""
    full = article(id, title, "")
    keep = ("type_of", "id", "title", "description", "slug", "path", "url", "canonical_url", "published_at",
            "published_timestamp", "tag_list", "reading_time_minutes", "user")
    return {**{k: full[k] for k in keep}, "canonical_url": canonical or full["url"], "created_at": "2026-10-30T08:59:00Z",
            "edited_at": None, "crossposted_at": None, "last_comment_at": None, "readable_publish_date": "Oct 30",
            "social_image": "", "tags": "", "cover_image": None}


def answering(listing: list[dict]) -> tuple[DevtoPlatform, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=listing)

    return DevtoPlatform(httpx2.AsyncClient(transport=httpx2.MockTransport(handler))), requests


class Lookup(unittest.TestCase):
    def find(self, listing: list[dict], fields: dict | None = None, body: str = "Body."):
        devto, requests = answering(listing)
        prepared = devto.prepare({"title": "Hello", **(fields or {})}, body)
        found = asyncio.run(devto.find_existing({"api_key": "k"}, Account(id="42", name="On Call"), prepared))
        return found, requests

    def assert_cannot_tell(self, found, *named: str) -> str:
        self.assertIsNotNone(found, "read as missing: the post would be sent again")
        self.assertEqual((found.url, found.remote_id), (None, None), "an article was taken for the post")
        (reason,) = found.notes
        for name in named:
            self.assertIn(name, reason)
        return reason


class FindingAnOlderArticleWithTheSameTitleIsNotThePost(Lookup):
    def test_an_older_draft_with_the_same_title_alone_is_not_returned(self):
        """The probe of the review of #390 (V1): an older draft with the post's title, the send's request not arrived."""
        found, requests = self.find([article(1001, "Hello", "An older piece.", published=False)])
        reason = self.assert_cannot_tell(found, "id 1001", "other content")
        self.assertIn("DEV can't say which article is this post", reason)
        self.assertEqual((requests[0].url.path, requests[0].url.params["per_page"]), ("/api/articles/me/all", "100"))

    def test_the_sends_own_article_is_found_beside_an_older_one(self):
        """Drafts come first in DEV's order: the older draft is listed before the article this send made."""
        found, _ = self.find([article(1001, "Hello", "An older piece.", published=False), article(1002, "Hello", "Body.\n")])
        self.assertEqual((found.url, found.remote_id, found.status), ("https://dev.to/on-call/hello-1002", "1002", "published"))

    def test_the_canonical_url_names_the_post_over_an_older_article_with_its_title(self):
        canonical = "https://blog.example/hello"
        found, _ = self.find([article(1001, "Hello", "An older piece."), article(1002, "Hello", "Body.\n", canonical=canonical)],
                             {"canonical_url": canonical})
        self.assertEqual((found.url, found.remote_id), ("https://dev.to/on-call/hello-1002", "1002"))

    def test_an_article_with_the_canonical_url_but_another_title_or_body_cannot_be_told(self):
        """A first copy whose file was edited since: the person removes it, or it is this post as DEV re-rendered it."""
        canonical = "https://blog.example/hello"
        for other in (article(1001, "Hello, again", "Body.\n", canonical=canonical),
                      article(1001, "Hello", "An older piece.", canonical=canonical)):
            with self.subTest(title=other["title"]):
                found, _ = self.find([other], {"canonical_url": canonical})
                self.assert_cannot_tell(found, "id 1001")


class FindingWhatCannotBeToldIsLeftToThePerson(Lookup):
    def test_two_articles_with_the_posts_content_cannot_be_told_apart(self):
        """A first copy the person was told to remove, and the send's own: nothing says which is the extra one."""
        found, _ = self.find([article(1002, "Hello", "Body."), article(1001, "Hello", "Body.\n")])
        self.assert_cannot_tell(found, "id 1002", "id 1001", "2 articles")

    def test_an_answer_without_a_body_finds_one_article_by_its_title_and_cannot_tell_two_apart(self):
        """An answer with the documented fields only (no body) gives nothing beyond the title to compare: one article
        with the title is the post, as before (the existing recovery tests' fakes list titles alone); two can't be told."""
        found, _ = self.find([documented(1001, "Hello")])
        self.assertEqual((found.url, found.remote_id, found.status), ("https://dev.to/on-call/hello-1001", "1001", "published"))
        found, _ = self.find([documented(1002, "Hello"), documented(1001, "Hello")])
        self.assert_cannot_tell(found, "id 1002", "id 1001", "2 articles")

    def test_a_canonical_url_match_without_a_body_is_enough(self):
        canonical = "https://blog.example/hello"
        found, _ = self.find([documented(1001, "Hello", canonical=canonical)], {"canonical_url": canonical})
        self.assertEqual((found.url, found.remote_id, found.status), ("https://dev.to/on-call/hello-1001", "1001", "published"))

    def test_nothing_with_the_title_or_the_canonical_url_is_missing(self):
        found, _ = self.find([article(1001, "Another", "An older piece."), article(1002, "Hello again", "Body.")],
                             {"canonical_url": "https://blog.example/hello"})
        self.assertIsNone(found)

    def test_an_answer_without_a_list_cannot_tell_and_an_article_without_an_id_names_nothing(self):
        self.assert_cannot_tell(self.find({"error": "odd"})[0], "not a list")
        found, _ = self.find([{**article(1001, "Hello", "Body."), "id": None}])
        self.assertEqual((found.url, found.remote_id), ("https://dev.to/on-call/hello-1001", None))


class FindingRecoveryLeavesTheDoubtToThePerson(unittest.TestCase):
    """The interrupted send of the probe: the post was recorded 'draft' on the older article (remote_id 1001). Now the
    platform can't tell, and the post is unconfirmed for the person, with the reason in the log."""

    def test_an_interrupted_send_with_an_older_draft_of_the_same_title_is_unconfirmed(self):
        store = Store(tempfile.mkdtemp() + "/inkcast.db", "k" * 40)
        self.addCleanup(store.close)
        user = store.upsert_user("local:on-call", "on-call", None, None)["id"]
        devto, _ = answering([article(1001, "Hello", "An older piece.", published=False)])
        store.save_credentials(user, devto.id, {"api_key": "k"}, {"id": "42", "name": "On Call"})
        prepared = devto.prepare({"title": "Hello"}, "Body.")
        post_id = store.create_post(group_id="g", user_id=user, platform=devto.id, title="Hello", status="publishing",
                                    run_at=time.time() - STUCK_AFTER - 2, payload=prepared.to_dict())
        store._exec("UPDATE posts SET updated_at = updated_at - ? WHERE id = ?", STUCK_AFTER + 1, post_id)
        quiet = logging.getLogger("inkcast.libs.pipeline")
        with self.assertLogs(quiet, level="WARNING") as logged:
            (outcome,) = asyncio.run(Publisher(store, {devto.id: devto}).run_due(now=time.time()))
        post = store.get_post(post_id)
        self.assertEqual((outcome.status, post["status"], post["remote_id"], post["result_url"]),
                         ("unconfirmed", "unconfirmed", None, None), "the older draft was recorded as the post")
        self.assertIn("can't be checked automatically", post["error"])
        self.assertTrue(any("id 1001" in r.getMessage() and "other content" in r.getMessage() for r in logged.records),
                        "the reason DEV could not tell is not in the log")


if __name__ == "__main__":
    unittest.main()

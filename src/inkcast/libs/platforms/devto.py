# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""DEV (dev.to) and other Forem communities, through the Forem API v1.

Rules encoded here:
* The title is its own field, so the body must not repeat it.
* ``description`` is the subtitle/summary shown in feeds and link previews.
* Up to 4 tags, lowercase letters and digits only.
* ``organization_id`` posts under an organization the account belongs to.
* No scheduling via the API: Inkcast schedules on its side.

Set INKCAST_API_URL_DEVTO to target another Forem instance.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .base import Account, CredentialField, Destination, Platform, PlatformError, Prepared, PublishResult

MAX_TAGS = 4
MAX_TITLE = 128
# Articles per page of GET /articles/me/all that find_existing reads: one page, newest first.
LOOKUP_PAGE = 100


def _normalise_tag(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tag.lower())


class DevtoPlatform(Platform):
    id = "devto"
    name = "DEV"
    default_api_url = "https://dev.to/api"
    destination_label = "organization"
    credential_fields = [
        CredentialField(
            key="api_key",
            label="API key",
            help="dev.to → Settings → Extensions → DEV Community API Keys → Generate API Key.",
        )
    ]
    connect_help = "Paste a DEV API key. It lets Inkcast create articles as you."
    capabilities = frozenset({"update", "find"})

    @staticmethod
    def _headers(creds: dict[str, str]) -> dict[str, str]:
        return {"api-key": creds["api_key"], "Accept": "application/vnd.forem.api-v1+json", "Content-Type": "application/json"}

    @property
    def site_url(self) -> str:
        parsed = urlparse(self.api_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def describe_error(self, response) -> str:
        if response.status_code == 401:
            return "DEV rejected the API key (revoked or invalid). Reconnect DEV in Inkcast."
        try:
            body = response.json()
            detail = body.get("error") or body.get("errors") or body
        except (ValueError, AttributeError):  # not JSON, or JSON that isn't an object
            detail = response.text[:300]
        return f"DEV rejected the article ({response.status_code}): {detail}"

    async def verify(self, creds: dict[str, str]) -> Account:
        if not creds.get("api_key", "").strip():
            raise PlatformError("A DEV API key is required.")
        me = await self.request("GET", "/users/me", headers=self._headers(creds)) or {}
        if not me.get("id"):
            raise PlatformError("DEV didn't return an account for this key.")
        handle = me.get("username")
        return Account(id=str(me["id"]), name=me.get("name") or handle or "DEV", handle=handle,
                       url=f"{self.site_url}/{handle}" if handle else None)

    async def resolve_destination(self, creds: dict[str, str], account: Account, wanted: str) -> Destination:
        wanted = wanted.strip()
        if wanted.isdigit():
            return Destination(id=wanted, name=f"organization {wanted}")
        org = await self.request("GET", f"/organizations/{wanted.lstrip('@')}", headers=self._headers(creds)) or {}
        if not org.get("id"):
            raise PlatformError(
                f"DEV didn't return an id for organization {wanted!r}. Pass its numeric organization id instead."
            )
        return Destination(id=str(org["id"]), name=org.get("name") or wanted, url=f"{self.site_url}/{org.get('username', wanted)}")

    def prepare(self, fields: dict[str, Any], body: str) -> Prepared:
        title = fields["title"]
        warnings: list[str] = []
        if len(title) > MAX_TITLE:
            raise PlatformError(f"DEV titles must be {MAX_TITLE} characters or fewer (this one is {len(title)}).")

        tags: list[str] = []
        changed = []
        for raw in fields.get("tags", []):
            tag = _normalise_tag(raw)
            if tag != raw:
                changed.append(f"{raw}→{tag or '(dropped)'}")
            if tag and tag not in tags:
                tags.append(tag)
        if changed:
            warnings.append("DEV tags are lowercase letters and digits only: " + ", ".join(changed) + ".")
        if len(tags) > MAX_TAGS:
            warnings.append(f"DEV allows {MAX_TAGS} tags; kept {', '.join(tags[:MAX_TAGS])}.")
            tags = tags[:MAX_TAGS]

        published = not fields.get("draft")
        article: dict[str, Any] = {
            "title": title,
            "body_markdown": body.strip() + "\n",
            "published": published,
            "tags": tags,
        }
        if fields.get("subtitle"):
            article["description"] = fields["subtitle"]
        if fields.get("canonical_url"):
            article["canonical_url"] = fields["canonical_url"]
        if fields.get("cover_image"):
            article["main_image"] = fields["cover_image"]
        if fields.get("series"):
            article["series"] = str(fields["series"])

        destination = fields.get("destination") or fields.get("organization")
        destination = str(destination) if destination else None
        preview = {
            "title": title,
            "description": fields.get("subtitle") or "(none)",
            "tags": ", ".join(tags) or "(none)",
            "canonical_url": fields.get("canonical_url") or "(none)",
            "status": "published" if published else "draft",
            "organization": destination or "(your profile)",
        }
        if fields.get("series"):
            preview["series"] = fields["series"]
        return Prepared(platform=self.id, title=title, payload={"article": article}, preview=preview,
                        destination=destination, warnings=warnings)

    @staticmethod
    def _payload(prepared: Prepared, destination: Destination | None) -> dict[str, Any]:
        payload = {"article": dict(prepared.payload["article"])}
        if destination:
            payload["article"]["organization_id"] = int(destination.id)
        return payload

    @staticmethod
    def _result(data: dict[str, Any], payload: dict[str, Any]) -> PublishResult:
        published = data.get("published", payload["article"]["published"])
        return PublishResult(url=data.get("url"), remote_id=str(data["id"]) if data.get("id") else None,
                             status="published" if published else "draft")

    async def publish(self, creds: dict[str, str], account: Account, prepared: Prepared,
                      destination: Destination | None) -> PublishResult:
        payload = self._payload(prepared, destination)
        data = await self.request("POST", "/articles", headers=self._headers(creds), json=payload) or {}
        return self._result(data, payload)

    async def update(self, creds: dict[str, str], account: Account, remote_id: str, prepared: Prepared,
                     destination: Destination | None) -> PublishResult:
        payload = self._payload(prepared, destination)
        if not str(remote_id).isdigit():
            raise PlatformError(f"DEV article id {remote_id!r} isn't valid.")
        data = await self.request("PUT", f"/articles/{remote_id}", headers=self._headers(creds), json=payload) or {}
        return self._result(data, payload)

    async def find_existing(self, creds: dict[str, str], account: Account, prepared: Prepared) -> PublishResult | None:
        """The article this send created, among the account's, or None when DEV has none with its title or canonical
        URL (the pipeline then sends the post again). An article is this send's when it agrees with what was sent on
        everything DEV lets Inkcast compare: the title; the canonical URL when the send set one; the body when the
        answer carries it, which tells an older article or draft with the same title apart (#396). Exactly one such
        article is the post. Otherwise DEV can't tell (more than one agree; an article shares the title or the canonical
        URL but not the rest: an older article, a first copy, or this post as DEV re-rendered it), and the result names
        neither URL nor id, with the reason in ``notes``: the pipeline leaves the post to a person (``Publisher.confirm``).

        DEV's answer (GET /articles/me/all, ``per_page`` articles, the newest first) is read as the ``me`` view emits it
        (forem/forem@d4190ce, app/views/api/v1/articles/me.json.jbuilder): ``title``, ``url``, ``canonical_url`` (the
        article's own URL when none was set: ``processed_canonical_url``), ``published_at``, and, beyond the documented
        ``ArticleIndex`` (swagger/v1/api_v1.json@89422b0), ``published`` and ``body_markdown``; not the documented
        ``created_at``. So the body is compared only when present, and the status read from ``published`` when
        present, from ``published_at`` otherwise (``_listed``)."""
        article = prepared.payload["article"]
        title, body = article["title"], article["body_markdown"].strip()
        canonical = (article.get("canonical_url") or "").strip() or None
        mine = await self.request("GET", "/articles/me/all", headers=self._headers(creds), params={"per_page": LOOKUP_PAGE}) or []
        same: list[dict[str, Any]] = []
        unsure: list[str] = []
        for a in mine if isinstance(mine, list) else []:
            same_title = a.get("title") == title
            same_canonical = canonical is not None and (a.get("canonical_url") or "").strip() == canonical
            theirs = a.get("body_markdown")
            same_body = theirs is not None and theirs.strip() == body
            # Without a canonical URL, the body too: DEV stores the title as it normalises it (characters it doesn't
            # allow, such as an arrow, become spaces, and whitespace is collapsed), so the post's own article can list
            # under another title. A canonical URL the send set is stored as sent, and names the article whatever its title.
            if not same_title and not same_canonical and not (same_body and canonical is None):
                continue
            differs = ([] if same_title else ["another title"]) \
                + (["another canonical URL"] if canonical is not None and not same_canonical else []) \
                + (["other content"] if theirs is not None and not same_body else []) \
                + ([] if self._published(a) == article["published"] else ["another status"])
            if differs:
                unsure.append(f"id {a.get('id')} ({', '.join(differs)})")
            else:
                same.append(a)
        if len(same) == 1:
            return self._listed(same[0])
        if not same and not unsure and len(mine) < LOOKUP_PAGE:
            return None
        if not same and not unsure:
            # DEV lists drafts first (published_at DESC puts NULL first): past a full page of them, a published
            # send's article is on a page not read, so "none here" is not "missing".
            reason = f"none of the {len(mine)} articles DEV listed (a full page) is it, and the rest were not read"
        elif same:
            reason = f"{len(same)} articles agree with it: " + ", ".join(f"id {a.get('id')}" for a in same)
        else:
            reason = (f"{'an article shares' if len(unsure) == 1 else 'articles share'} its title or canonical URL but not "
                      f"the rest: {', '.join(unsure)}")
        return PublishResult(url=None, remote_id=None, status="published",
                             notes=[f"DEV can't say which article is this post: {reason}."])

    @staticmethod
    def _published(a: dict[str, Any]) -> bool:
        """Whether a listed article is published: ``published`` where the ``me`` view gives it; the documented listing
        has ``published_at`` instead, which an article has once published. DEV makes an article as the send asked
        (``published``), so a draft is never a published send's article, nor the other way round."""
        return bool(a["published"] if "published" in a else a.get("published_at"))

    @classmethod
    def _listed(cls, a: dict[str, Any]) -> PublishResult:
        """A listed article as a result."""
        return PublishResult(url=a.get("url"), remote_id=str(a["id"]) if a.get("id") else None,
                             status="published" if cls._published(a) else "draft")

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
        except ValueError:
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

    async def publish(self, creds: dict[str, str], account: Account, prepared: Prepared,
                      destination: Destination | None) -> PublishResult:
        payload = {"article": dict(prepared.payload["article"])}
        if destination:
            payload["article"]["organization_id"] = int(destination.id)
        data = await self.request("POST", "/articles", headers=self._headers(creds), json=payload) or {}
        published = data.get("published", payload["article"]["published"])
        return PublishResult(url=data.get("url"), remote_id=str(data["id"]) if data.get("id") else None,
                             status="published" if published else "draft")

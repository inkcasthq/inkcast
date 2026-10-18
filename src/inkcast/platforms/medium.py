# SPDX-License-Identifier: PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Medium, through its official REST API and a self-issued integration token.

Medium's API rules this plug-in encodes:
* The ``title`` field is metadata only; the title must also be in the content
  to show on the page. There is no subtitle field: a small heading right under
  the title is how Medium displays a subtitle.
* Only the first 3 tags are used; tags over 25 characters are ignored.
* There is no cover-image field: the first image in the story becomes the
  featured image.
* In a publication, editors may publish directly; writers can only create
  drafts that an editor approves.
* No scheduling, editing or deleting: Inkcast schedules on its side.
"""

from __future__ import annotations

from typing import Any

from .base import Account, CredentialField, Destination, Platform, PlatformError, Prepared, PublishResult

MAX_TAGS = 3
MAX_TAG_LEN = 25
STATUSES = {"public", "draft", "unlisted"}
LICENSES = {
    "all-rights-reserved", "cc-40-by", "cc-40-by-sa", "cc-40-by-nd", "cc-40-by-nc",
    "cc-40-by-nc-nd", "cc-40-by-nc-sa", "cc-40-zero", "public-domain",
}


class MediumPlatform(Platform):
    id = "medium"
    name = "Medium"
    default_api_url = "https://api.medium.com/v1"
    destination_label = "publication"
    credential_fields = [
        CredentialField(
            key="token",
            label="Integration token",
            help="Medium → Settings → Integration tokens. Medium stopped issuing new tokens on 1 Jan 2025; "
                 "tokens created before then still work.",
        )
    ]
    connect_help = (
        "Paste a Medium integration token. If your Medium settings have no Integration tokens section, your "
        "account can't use Medium's API; Inkcast will instead give you the steps to import each story by URL."
    )

    @staticmethod
    def _headers(creds: dict[str, str]) -> dict[str, str]:
        return {"Authorization": f"Bearer {creds['token']}", "Accept": "application/json", "Content-Type": "application/json"}

    def describe_error(self, response) -> str:
        if response.status_code == 401:
            return "Medium rejected the integration token (revoked or invalid). Reconnect Medium in Inkcast."
        if response.status_code == 403:
            return "Medium refused: this account can't publish there (wrong publication, or not a writer/editor)."
        try:
            errors = response.json().get("errors") or []
            detail = "; ".join(e.get("message", "") for e in errors) or response.text[:300]
        except ValueError:
            detail = response.text[:300]
        return f"Medium rejected the story ({response.status_code}): {detail}"

    async def verify(self, creds: dict[str, str]) -> Account:
        if not creds.get("token", "").strip():
            raise PlatformError("A Medium integration token is required.")
        data = (await self.request("GET", "/me", headers=self._headers(creds)) or {}).get("data") or {}
        if not data.get("id"):
            raise PlatformError("Medium didn't return an account for this token.")
        return Account(id=data["id"], name=data.get("name") or data.get("username") or "Medium",
                       handle=data.get("username"), url=data.get("url"))

    async def destinations(self, creds: dict[str, str], account: Account) -> list[Destination]:
        data = (await self.request("GET", f"/users/{account.id}/publications", headers=self._headers(creds)) or {}).get("data") or []
        return [Destination(id=p["id"], name=p.get("name") or p["id"], url=p.get("url")) for p in data]

    async def resolve_destination(self, creds: dict[str, str], account: Account, wanted: str) -> Destination:
        dest = await super().resolve_destination(creds, account, wanted)
        contributors = (await self.request(
            "GET", f"/publications/{dest.id}/contributors", headers=self._headers(creds)
        ) or {}).get("data") or []
        role = next((c.get("role") for c in contributors if c.get("userId") == account.id), None)
        if role not in ("editor", "writer"):
            raise PlatformError(f"Your Medium account isn't a writer or editor of {dest.name}, so it can't post there.")
        dest.note = role
        return dest

    def prepare(self, fields: dict[str, Any], body: str) -> Prepared:
        title = fields["title"]
        warnings: list[str] = []
        if len(title) > 100:
            raise PlatformError(f"Medium titles must be 100 characters or fewer (this one is {len(title)}).")

        tags = [t for t in fields.get("tags", [])]
        long_tags = [t for t in tags if len(t) > MAX_TAG_LEN]
        if long_tags:
            warnings.append(f"Medium ignores tags over {MAX_TAG_LEN} characters; dropped {', '.join(long_tags)}.")
            tags = [t for t in tags if len(t) <= MAX_TAG_LEN]
        if len(tags) > MAX_TAGS:
            warnings.append(f"Medium uses only the first {MAX_TAGS} tags; kept {', '.join(tags[:MAX_TAGS])}.")
            tags = tags[:MAX_TAGS]

        status = "draft" if fields.get("draft") else str(fields.get("publish_status", "public")).lower()
        if status not in STATUSES:
            raise PlatformError(f"medium.publish_status must be one of {', '.join(sorted(STATUSES))}.")

        parts = [f"# {title}"]
        subtitle = fields.get("subtitle")
        if subtitle and fields.get("subtitle_in_body", True):
            parts.append(f"## {subtitle}")
        if fields.get("cover_image"):
            parts.append(f"![]({fields['cover_image']})")
        parts.append(body.strip())
        content = "\n\n".join(parts) + "\n"

        payload: dict[str, Any] = {
            "title": title,
            "contentFormat": "markdown",
            "content": content,
            "tags": tags,
            "publishStatus": status,
        }
        if fields.get("canonical_url"):
            payload["canonicalUrl"] = fields["canonical_url"]
        if fields.get("license"):
            if fields["license"] not in LICENSES:
                raise PlatformError(f"medium.license must be one of {', '.join(sorted(LICENSES))}.")
            payload["license"] = fields["license"]
        if "notify_followers" in fields:
            payload["notifyFollowers"] = bool(fields["notify_followers"])

        destination = fields.get("destination") or fields.get("publication")
        preview = {
            "title": title,
            "subtitle": subtitle if subtitle and fields.get("subtitle_in_body", True) else "(none)",
            "tags": ", ".join(tags) or "(none)",
            "canonical_url": fields.get("canonical_url") or "(none)",
            "status": status,
            "publication": destination or "(your profile)",
        }
        return Prepared(platform=self.id, title=title, payload=payload, preview=preview,
                        destination=destination, warnings=warnings)

    async def publish(self, creds: dict[str, str], account: Account, prepared: Prepared,
                      destination: Destination | None) -> PublishResult:
        payload = dict(prepared.payload)
        notes: list[str] = []
        if destination and destination.note == "writer" and payload["publishStatus"] != "draft":
            payload["publishStatus"] = "draft"
            notes.append(f"You're a writer (not an editor) in {destination.name}, so Medium created a draft for the "
                         "editors to approve.")
        path = f"/publications/{destination.id}/posts" if destination else f"/users/{account.id}/posts"
        data = (await self.request("POST", path, headers=self._headers(creds), json=payload) or {}).get("data") or {}
        return PublishResult(url=data.get("url"), remote_id=data.get("id"),
                             status=data.get("publishStatus") or payload["publishStatus"], notes=notes)

    def not_connected_help(self, fields: dict[str, Any]) -> str | None:
        source = fields.get("canonical_url")
        steps = (
            "Medium no longer issues API tokens, so Inkcast can't post to Medium for this account. Use Medium's "
            "import instead: on medium.com open Stories → Import a story, paste the URL of the published original"
        )
        if source:
            steps += f" ({source})"
        return steps + ", then review and publish. Medium sets the canonical link back to the original automatically."

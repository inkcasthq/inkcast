# SPDX-License-Identifier: PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Use cases, shared by the MCP tools and the REST API. Every method takes the
authenticated user id first; nothing here trusts input about *who* is calling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import (
    ConnectedAccount, ConnectLink, DestinationInfo, Me, PlatformInfo, PlatformResult, PostInfo,
    PublishRequest, PublishResponse,
)
from .platforms import Account, Platform, PlatformError
from .publisher import Publisher
from .storage import Store
from .story import StoryError, parse_story

TICKET_TTL = 900


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class InkcastService:
    def __init__(self, store: Store, platforms: dict[str, Platform], publisher: Publisher, public_url: str):
        self.store, self.platforms, self.publisher, self.public_url = store, platforms, publisher, public_url

    def _platform(self, pid: str) -> Platform:
        p = self.platforms.get(pid.strip().lower())
        if not p:
            raise ServiceError(404, "unknown_platform", f"Unknown platform {pid!r}. Available: {', '.join(self.platforms)}.")
        return p

    def me(self, user_id: str) -> Me:
        u = self.store.get_user(user_id) or {}
        return Me(id=user_id, display=u.get("display", "?"), email=u.get("email"), name=u.get("name"))

    def list_platforms(self, user_id: str) -> list[PlatformInfo]:
        connected = self.store.connected_accounts(user_id)
        return [
            PlatformInfo(id=pid, name=p.name, connected=pid in connected,
                         account=ConnectedAccount(**connected[pid]) if pid in connected else None,
                         destination_label=p.destination_label)
            for pid, p in self.platforms.items()
        ]

    def connect_link(self, user_id: str, pid: str) -> ConnectLink:
        p = self._platform(pid)
        ticket = self.store.create_ticket(user_id, p.id, ttl=TICKET_TTL)
        return ConnectLink(platform=p.id, url=f"{self.public_url}/connect/{ticket}", expires_in=TICKET_TTL, help=p.connect_help)

    def disconnect(self, user_id: str, pid: str) -> bool:
        return self.store.delete_credentials(user_id, self._platform(pid).id)

    async def destinations(self, user_id: str, pid: str) -> list[DestinationInfo]:
        p = self._platform(pid)
        if not p.destination_label:
            return []
        connected = self.store.get_credentials(user_id, p.id)
        if not connected:
            raise ServiceError(409, "not_connected", f"{p.name} isn't connected; create a connect link first.")
        creds, acct = connected
        try:
            return [DestinationInfo(id=d.id, name=d.name, url=d.url) for d in await p.destinations(creds, Account.from_dict(acct))]
        except PlatformError as exc:
            raise ServiceError(502, "platform_error", str(exc)) from exc

    async def publish(self, user_id: str, req: PublishRequest) -> PublishResponse:
        try:
            story = parse_story(
                req.markdown, known_platforms=self.platforms.keys(), title=req.title, subtitle=req.subtitle,
                tags=req.tags, canonical_url=req.canonical_url, cover_image=req.cover_image, platforms=req.platforms,
                destinations=req.destinations, publish_at=req.publish_at, draft=req.draft,
                image_base_url=req.image_base_url,
            )
        except StoryError as exc:
            raise ServiceError(422, "invalid_story", str(exc)) from exc

        connected = self.store.connected_accounts(user_id)
        targets = story.platforms or [pid for pid in connected if pid in self.platforms]
        if not targets:
            raise ServiceError(422, "no_platforms", "Say which platforms to post to, or connect one first.")

        results: list[PlatformResult] = []
        if req.dry_run:
            ready, problems = self.publisher.prepare(story, targets)
            for prep in ready:
                p = self.platforms[prep.platform]
                is_connected = prep.platform in connected
                help_text = None if is_connected else p.not_connected_help(story.for_platform(p.id))
                results.append(PlatformResult(platform=prep.platform, status="preview", preview=prep.preview,
                                              warnings=prep.warnings, connected=is_connected, message=help_text))
            results += [PlatformResult(platform=o.platform, status="failed", message=o.message) for o in problems]
        else:
            for o in await self.publisher.publish(user_id, story, targets):
                results.append(PlatformResult(
                    platform=o.platform, status=o.status, post_id=o.post_id, url=o.url, message=o.message,
                    notes=o.notes, warnings=o.prepared.warnings if o.prepared else []))
        order = {pid: i for i, pid in enumerate(targets)}
        results.sort(key=lambda r: order.get(r.platform, 99))
        return PublishResponse(title=story.title, subtitle=story.subtitle, dry_run=req.dry_run,
                               publish_at=story.publish_at.isoformat() if story.publish_at else None,
                               warnings=story.warnings, results=results)

    def list_posts(self, user_id: str, days_back: int = 7, days_ahead: int = 90) -> list[PostInfo]:
        now = datetime.now(timezone.utc)
        rows = self.store.list_posts(user_id, (now - timedelta(days=days_back)).timestamp(),
                                     (now + timedelta(days=days_ahead)).timestamp())
        return [self._post(r) for r in rows]

    def cancel(self, user_id: str, post_id: str) -> PostInfo:
        try:
            self.publisher.cancel(user_id, post_id)
        except PlatformError as exc:
            status = 404 if str(exc).startswith("No post") else 409
            raise ServiceError(status, "not_found" if status == 404 else "not_cancellable", str(exc)) from exc
        return self._post(self.store.get_post(post_id))

    @staticmethod
    def _post(r: dict) -> PostInfo:
        return PostInfo(id=r["id"], group_id=r["group_id"], platform=r["platform"], title=r["title"], status=r["status"],
                        run_at=datetime.fromtimestamp(r["run_at"], timezone.utc).isoformat(timespec="seconds"),
                        url=r["result_url"], error=r["error"], attempts=r["attempts"])

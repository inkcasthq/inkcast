# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Request/response models shared by the MCP tools and the REST API (and its OpenAPI document)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..content.story import SCHEDULE_HORIZON_DAYS


class ErrorBody(BaseModel):
    error: str = Field(description="Machine-readable error code.")
    message: str = Field(description="What went wrong and how to fix it.")


class Me(BaseModel):
    id: str
    display: str
    email: str | None = None
    name: str | None = None


class ConnectedAccount(BaseModel):
    id: str
    name: str
    handle: str | None = None
    url: str | None = None


class PlatformInfo(BaseModel):
    id: str = Field(description="Platform id used in requests and front matter, e.g. `devto`.")
    name: str
    connected: bool
    account: ConnectedAccount | None = None
    destination_label: str | None = Field(None, description="What a destination is called here, e.g. `publication`.")


class ConnectLink(BaseModel):
    platform: str
    url: str = Field(description="One-time page where the person enters this platform's credentials.")
    expires_in: int = Field(description="Seconds until the link expires.")
    help: str


class DeletionLink(BaseModel):
    url: str = Field(description="One-time page where the person, after signing in again, starts deleting their account.")
    expires_in: int = Field(description="Seconds until the link expires.")


class DestinationInfo(BaseModel):
    id: str
    name: str
    url: str | None = None


class PublishRequest(BaseModel):
    markdown: str = Field(description="Full markdown text of the story, including optional YAML front matter.")
    platforms: list[str] | None = Field(None, description="Platform ids. Defaults to front matter `platforms`, then all connected.")
    title: str | None = None
    subtitle: str | None = None
    tags: list[str] | None = None
    canonical_url: str | None = Field(None, description="URL of the original post; set as the canonical link everywhere.")
    cover_image: str | None = None
    destinations: dict[str, str] | None = Field(None, description="Per-platform destination, e.g. `{\"medium\": \"Better Programming\"}`.")
    publish_at: str | None = Field(None, description=f"ISO 8601 with a UTC offset, e.g. `2026-10-21T09:00:00-05:00`, at most "
                                                     f"{SCHEDULE_HORIZON_DAYS} days ahead. Omit to publish now.")
    draft: bool = False
    image_base_url: str | None = None
    dry_run: bool = Field(False, description="Validate and preview only; nothing is sent or stored.")


PostStatus = Literal["preview", "scheduled", "publishing", "recovering", "published", "draft", "unlisted", "failed",
                     "skipped", "unconfirmed", "cancelled"]


class PlatformResult(BaseModel):
    platform: str
    status: PostStatus
    post_id: str | None = None
    url: str | None = None
    message: str | None = None
    notes: list[str] = []
    warnings: list[str] = []
    preview: dict[str, Any] | None = None
    connected: bool | None = None


class PublishResponse(BaseModel):
    title: str
    subtitle: str | None = None
    dry_run: bool
    publish_at: str | None = None
    warnings: list[str] = []
    results: list[PlatformResult]


class PostInfo(BaseModel):
    id: str
    group_id: str
    platform: str
    title: str
    status: PostStatus
    run_at: str
    url: str | None = None
    error: str | None = None
    attempts: int = 0


# ---- personal access tokens (IdentityService) --------------------------------------------


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="What the token is for, e.g. `blog repo CI`.")
    scopes: list[str] = Field(min_length=1, description="Scopes, e.g. `[\"stories:sync\"]`. See the API description.")
    # 366 is the absolute ceiling. The server applies its own policy where the token is made: omitted means its
    # default (INKCAST_PAT_DEFAULT_DAYS, 90 unless configured), its maximum may be lower (INKCAST_PAT_MAX_DAYS), and
    # null is refused where tokens must expire (INKCAST_PAT_ALLOW_NO_EXPIRY=false); each refusal is a 422 naming it.
    expires_in_days: int | None = Field(90, ge=1, le=366, description=(
        "Days until it stops working. Omit for the server's default (90 unless configured); the server's maximum may "
        "be lower than 366. null = never, where the server allows it. A refusal is a 422 naming the limit."))


class TokenInfo(BaseModel):
    id: str
    name: str
    scopes: list[str]
    created_at: str
    expires_at: str | None = None
    last_used_at: str | None = None


class TokenCreated(TokenInfo):
    token: str = Field(description="The token. Shown only once; store it as a CI secret (e.g. `INKCAST_TOKEN`).")


# ---- Git publishing: stories sync and the publish ledger (PublishingApi) -------------------


class SyncFile(BaseModel):
    path: str = Field(min_length=1, max_length=500, description="Path of the file in the repository, e.g. `posts/hello.md`.")
    markdown: str = Field(description="Full text of the file, including YAML front matter.")
    image_base_url: str | None = Field(None, description="Base URL for this file's relative images (overrides the "
                                       "request's), e.g. the raw URL of the file's directory at this commit.")


class SyncRequest(BaseModel):
    source: str = Field(min_length=1, max_length=300,
                        description="Stable id of the repository, e.g. `github.com/acme/blog`. Ledger entries are keyed by it.")
    ref: str | None = Field(None, max_length=300, description="Commit or ref being synced, for the record.")
    files: list[SyncFile] = Field(min_length=1, max_length=50)
    platforms: list[str] | None = Field(None, description="Override the platforms in each file's front matter.")
    image_base_url: str | None = Field(None, description="Base URL that relative image paths resolve against.")
    dry_run: bool = Field(False, description="Report what would happen and preview each platform; change nothing.")


SyncAction = Literal["create", "update", "unchanged", "stale", "skipped", "failed"]


class SyncResult(BaseModel):
    path: str
    platform: str
    action: SyncAction = Field(description="create: new post. update: post edited in place. unchanged: content "
                               "already synced. stale: changed, but the platform can't edit posts, so it was "
                               "left alone. skipped: not connected or opted out. failed: see message.")
    status: str | None = Field(None, description="Post status after the sync (published, scheduled, draft...).")
    post_id: str | None = None
    url: str | None = None
    message: str | None = None
    preview: dict[str, Any] | None = None


class SyncResponse(BaseModel):
    source: str
    ref: str | None = None
    dry_run: bool
    results: list[SyncResult]


class LedgerEntry(BaseModel):
    source: str
    path: str
    platform: str
    status: str
    content_hash: str
    post_id: str | None = None
    url: str | None = None
    message: str | None = None
    synced_at: str


# ---- insights (InsightsService) ---------------------------------------------------------------


class RateSummary(BaseModel):
    platform: str
    numerator: float
    denominator: float
    rate: float | None = Field(None, description="numerator / denominator, or null when nothing happened.")


class InsightsSummary(BaseModel):
    days: int
    scope: str = Field(description="What the numbers are about: `user` now; `workspace` or `org` with E3.")
    scope_id: str
    posts_by_status: dict[str, dict[str, int]] = Field(description="status -> platform -> posts that changed in the period "
                                                       "(a post scheduled for later counts as scheduled now).")
    ledger_by_status: dict[str, int] = Field(description="Git publishing ledger entries by status, now.")
    publish_success: list[RateSummary] = Field(description="published / (published + failed + unconfirmed), per platform.")
    on_time: list[RateSummary] = Field(description="scheduled sends within two minutes of their time, per platform.")
    sync_by_action: dict[str, int]
    sync_by_platform: dict[str, int]


class KpiPoint(BaseModel):
    bucket: int = Field(description="Start of the bucket, UTC epoch seconds.")
    dims: dict[str, str]
    value: float
    n: int = Field(description="Events behind the value (value / n is the mean for duration KPIs).")


class KpiSeries(BaseModel):
    name: str
    unit: str
    grain: str
    points: list[KpiPoint]

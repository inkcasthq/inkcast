"""Request/response models shared by the MCP tools and the REST API (and its OpenAPI document)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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
    publish_at: str | None = Field(None, description="ISO 8601 with a UTC offset, e.g. `2026-10-21T09:00:00-05:00`. Omit to publish now.")
    draft: bool = False
    image_base_url: str | None = None
    dry_run: bool = Field(False, description="Validate and preview only; nothing is sent or stored.")


PostStatus = Literal["preview", "scheduled", "publishing", "published", "draft", "unlisted", "failed",
                     "skipped", "unknown", "cancelled"]


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

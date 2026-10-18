# SPDX-License-Identifier: PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""A platform-neutral story parsed from markdown with optional YAML front matter.

The story body never contains the title or subtitle: each platform plug-in
decides how to present them (Medium needs them in the body, Dev.to has
separate fields).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import yaml

_FRONT_MATTER = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_H2 = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_MD_IMAGE = re.compile(r"(!\[[^\]]*\]\()\s*<?([^)\s>]+)>?((?:\s+[\"'][^\"']*[\"'])?\s*\))")
_HTML_IMAGE = re.compile(r"(<img\b[^>]*?\bsrc\s*=\s*[\"'])([^\"']+)([\"'])", re.I)

# Common front-matter spellings mapped to Inkcast's names.
_ALIASES = {
    "title": ("title",),
    "subtitle": ("subtitle", "description", "summary"),
    "tags": ("tags", "keywords"),
    "canonical_url": ("canonical_url", "canonical", "canonicalUrl"),
    "cover_image": ("cover_image", "cover", "image", "main_image"),
    "publish_at": ("publish_at",),
    "image_base_url": ("image_base_url",),
    "platforms": ("platforms",),
}


class StoryError(ValueError):
    """Input that can't become a story; the message says how to fix it."""


@dataclass
class Story:
    title: str
    subtitle: str | None
    body: str
    tags: list[str] = field(default_factory=list)
    canonical_url: str | None = None
    cover_image: str | None = None
    publish_at: datetime | None = None  # UTC; None = now
    draft: bool = False
    platforms: list[str] = field(default_factory=list)
    # Per-platform overrides from front matter blocks / the `destinations` argument.
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def for_platform(self, platform_id: str) -> dict[str, Any]:
        """Story fields with this platform's overrides applied."""
        merged: dict[str, Any] = {
            "title": self.title,
            "subtitle": self.subtitle,
            "tags": list(self.tags),
            "canonical_url": self.canonical_url,
            "cover_image": self.cover_image,
            "draft": self.draft,
        }
        for key, value in self.overrides.get(platform_id, {}).items():
            canonical_key = next((k for k, names in _ALIASES.items() if key in names), key)
            merged[canonical_key] = _as_tags(value) if canonical_key == "tags" else value
        return merged


def split_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER.match(markdown)
    if not match:
        return {}, markdown
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise StoryError(f"The front matter isn't valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise StoryError("The front matter must be a YAML mapping (key: value lines).")
    return data, markdown[match.end():]


def _pick(meta: dict[str, Any], name: str) -> Any:
    for key in _ALIASES[name]:
        value = meta.get(key)
        if value not in (None, "", []):
            return value
    return None


def _as_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        items: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise StoryError("tags must be a list or a comma-separated string.")
    tags: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("value") or item.get("label") or ""
        tag = str(item).strip().lstrip("#").strip()
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags


def check_url(url: str, what: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise StoryError(f"{what} must be a full http(s) URL, got {url!r}.")
    return str(url).strip()


def parse_when(value: Any, now: datetime) -> datetime:
    if isinstance(value, datetime):
        when = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            when = datetime.fromisoformat(text)
        except ValueError as exc:
            raise StoryError(
                f"publish_at {value!r} isn't an ISO 8601 date-time, e.g. 2026-10-21T09:00:00-05:00."
            ) from exc
    if when.tzinfo is None:
        raise StoryError(
            f"publish_at {value!r} has no time zone. Add a UTC offset, e.g. 2026-10-21T09:00:00-05:00."
        )
    when = when.astimezone(timezone.utc)
    if when < now + timedelta(minutes=2):
        raise StoryError(f"publish_at {when.isoformat()} is in the past or under 2 minutes away; omit it to publish now.")
    return when


def _strip_title_block(body: str, subtitle: str | None) -> tuple[str | None, str]:
    """Remove a leading `# Title` (returned) and a following `## Subtitle` that matches."""
    lines = body.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not (m := _H1.match(lines[i])):
        return None, "\n".join(lines[i:])
    h1 = m.group(1).strip()
    rest = lines[i + 1:]
    j = 0
    while j < len(rest) and not rest[j].strip():
        j += 1
    if subtitle and j < len(rest) and (m2 := _H2.match(rest[j])) and m2.group(1).strip().lower() == subtitle.lower():
        rest = rest[j + 1:]
    return h1, "\n".join(rest)


def _rewrite_images(body: str, base: str | None) -> tuple[str, list[str]]:
    unresolved: list[str] = []

    def fix(url: str) -> str:
        if urlparse(url).scheme in ("http", "https") or url.startswith("data:"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if base:
            return urljoin(base.rstrip("/") + "/", url)
        unresolved.append(url)
        return url

    body = _MD_IMAGE.sub(lambda m: m.group(1) + fix(m.group(2)) + m.group(3), body)
    body = _HTML_IMAGE.sub(lambda m: m.group(1) + fix(m.group(2)) + m.group(3), body)
    return body, unresolved


def parse_story(
    markdown: str,
    *,
    known_platforms: Iterable[str],
    title: str | None = None,
    subtitle: str | None = None,
    tags: list[str] | str | None = None,
    canonical_url: str | None = None,
    cover_image: str | None = None,
    platforms: list[str] | None = None,
    destinations: dict[str, str] | None = None,
    publish_at: str | None = None,
    draft: bool = False,
    image_base_url: str | None = None,
    now: datetime | None = None,
) -> Story:
    """Merge explicit arguments over front matter and validate the platform-neutral parts."""
    now = now or datetime.now(timezone.utc)
    known = list(known_platforms)
    if not markdown or not markdown.strip():
        raise StoryError("The markdown is empty. Read the file and pass its full contents.")

    meta, body = split_front_matter(markdown)
    subtitle = (subtitle or _pick(meta, "subtitle") or "").strip() or None
    h1, body = _strip_title_block(body, subtitle)
    title = (title or _pick(meta, "title") or h1 or "").strip()
    if len(title) < 2:
        raise StoryError("The story needs a title: pass `title`, add `title:` to the front matter, or start with `# Heading`.")

    canonical = canonical_url or _pick(meta, "canonical_url")
    cover = cover_image or _pick(meta, "cover_image")
    base = image_base_url or _pick(meta, "image_base_url")

    chosen = platforms or _as_tags(_pick(meta, "platforms")) or []
    chosen = [p.strip().lower() for p in chosen]
    unknown = [p for p in chosen if p not in known]
    if unknown:
        raise StoryError(f"Unknown platform(s): {', '.join(unknown)}. Available: {', '.join(known)}.")

    overrides: dict[str, dict[str, Any]] = {}
    for pid in known:
        block = meta.get(pid)
        if isinstance(block, dict):
            overrides[pid] = dict(block)
    for pid, dest in (destinations or {}).items():
        pid = pid.strip().lower()
        if pid not in known:
            raise StoryError(f"destinations has unknown platform {pid!r}. Available: {', '.join(known)}.")
        overrides.setdefault(pid, {})["destination"] = dest

    when_raw = publish_at or _pick(meta, "publish_at")
    when = parse_when(when_raw, now) if when_raw else None

    warnings: list[str] = []
    body, unresolved = _rewrite_images(body.strip() + "\n", check_url(base, "image_base_url") if base else None)
    if unresolved:
        shown = ", ".join(unresolved[:5]) + (" …" if len(unresolved) > 5 else "")
        warnings.append(
            "Platforms can only import images from public URLs; these are relative or local and will be broken: "
            f"{shown}. Pass image_base_url (e.g. the URL of the original post) to fix them."
        )

    return Story(
        title=title,
        subtitle=subtitle,
        body=body,
        tags=_as_tags(tags if tags not in (None, "", []) else _pick(meta, "tags")),
        canonical_url=check_url(canonical, "canonical_url") if canonical else None,
        cover_image=check_url(cover, "cover_image") if cover else None,
        publish_at=when,
        draft=draft,
        platforms=chosen,
        overrides=overrides,
        warnings=warnings,
    )

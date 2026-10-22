# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
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

# How far ahead a story may be scheduled, and how far ahead a listing can look (`days_ahead` on REST, MCP and in the
# OpenAPI document): one number, so every accepted schedule can be listed and cancelled (#95).
SCHEDULE_HORIZON_DAYS = 730

_FRONT_MATTER = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)
# The closing #s are trimmed by _heading, not here: a lazy group before \s*#*\s*$ backtracks cubically on spaces.
_H1 = re.compile(r"^#\s+(.+)$")
_H2 = re.compile(r"^##\s+(.+)$")
# Alt text stops at '[', the URL at '(', a tag at '<': otherwise one attempt can scan to the end of the body from every
# start, which is quadratic or worse (a 1 MB body took hours).
_MD_IMAGE = re.compile(r"(!\[[^\[\]]*\]\()\s*<?([^()\s>]+)>?((?:\s+[\"'][^\"']*[\"'])?\s*\))")
_HTML_IMAGE = re.compile(r"(<img\b[^<>]*?\bsrc\s*=\s*[\"'])([^\"']+)([\"'])", re.I)

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


class _RefusedValue(Exception):
    """A scalar PyYAML's resolver accepted but Python refused to make a value of: the node, Python's reason, and the
    key it sits under once the mapping holding it is known."""

    def __init__(self, node: yaml.ScalarNode, cause: Exception) -> None:
        super().__init__(str(cause))
        self.node, self.cause = node, cause
        self.field: str | None = None


def _guarded(construct):
    """One of SafeConstructor's scalar constructors, reporting a value Python refuses at its node."""

    def construct_or_report(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Any:
        # PyYAML also builds these from a mapping holding YAML 1.1's value key (`!!bool {=: yes}`), which front matter
        # doesn't need, and which would hand the message below a list of nodes instead of the scalar's text.
        if not isinstance(node, yaml.ScalarNode):
            raise yaml.constructor.ConstructorError(None, None, f"expected a scalar, but found a {node.id}", node.start_mark)
        # int() refuses decimal text over 4300 digits before converting it. PyYAML builds 1:2:3 (base 60) itself, in
        # time quadratic in the parts, on the event loop: the same limit on its text comes first, before it is built.
        if node.tag == "tag:yaml.org,2002:int" and ":" in node.value and len(node.value) > 4300:
            raise _RefusedValue(node, ValueError("a base-60 number over 4300 characters long"))
        try:
            value = construct(loader, node)
            if type(value) is int:
                str(value)  # int() limits decimal text to 4300 digits; 0x..., 0..., 0b... and 1:2:3 skip it, str() doesn't
            return value
        # Only the conversion of this one scalar runs in the try, so nothing unrelated is caught. It lets out
        # ValueError (datetime: 29 Feb 2027, an offset of 24 hours or more; int: 4301 digits, `!!int x`; float:
        # `!!float x`), KeyError (`!!bool x`), AttributeError (`!!timestamp x`: no regexp match, so None has
        # no groupdict) and IndexError (`!!int ''`, `!!float ''`: the sign check reads the first character); a
        # YAMLError of its own passes through to the caller as before.
        except (ValueError, KeyError, AttributeError, IndexError) as exc:
            raise _RefusedValue(node, exc) from exc

    return construct_or_report


class _FrontMatterLoader(yaml.SafeLoader):
    """SafeLoader whose refused values name their field. PyYAML lets the ValueError for `publish_at: 2027-02-29`
    escape safe_load, which was a 500 for the whole request instead of a StoryError for the one file (#107)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        try:
            return super().construct_mapping(node, deep)
        except _RefusedValue as exc:
            # A scalar is built inside the mapping loop, so one directly under a key is caught here, key in hand.
            # A list's items are built later by the document loop, so they stay unnamed and get their line instead.
            if exc.field is None:
                exc.field = next((k.value for k, v in node.value if v is exc.node and isinstance(k, yaml.ScalarNode)), None)
            raise


for _tag in ("timestamp", "int", "float", "bool"):
    _FrontMatterLoader.add_constructor(f"tag:yaml.org,2002:{_tag}", _guarded(getattr(yaml.SafeLoader, f"construct_yaml_{_tag}")))


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
            if canonical_key in ("title", "subtitle") and value is not None:
                value = str(value)  # YAML reads `title: 2048` as a number
            merged[canonical_key] = _as_tags(value) if canonical_key == "tags" else value
        return merged


def split_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER.match(markdown)
    if not match:
        return {}, markdown
    try:
        # An alias (*name) repeats a whole value: nested ones turn a few hundred bytes into gigabytes once the value
        # is made text (tags, titles, previews). Front matter doesn't need them, so they are refused before loading.
        # PyYAML composes nested lists and mappings recursively: a few thousand `[` are a RecursionError. The same walk
        # bounds the depth, before anything recurses.
        depth = 0
        for event in yaml.parse(match.group(1), Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise StoryError("The front matter uses a YAML alias (*name); write the value out instead.")
            depth += isinstance(event, yaml.CollectionStartEvent) - isinstance(event, yaml.CollectionEndEvent)
            if depth > 32:
                raise StoryError("The front matter nests lists or mappings more than 32 deep; flatten it.")
        # What yaml.load(text, Loader=_FrontMatterLoader) does; spelled out because CodeQL's model of yaml.load
        # knows SafeLoader itself as the safe Loader, not a subclass of it, and would report unsafe deserialization.
        loader = _FrontMatterLoader(match.group(1))
        try:
            data = loader.get_single_data() or {}
        finally:
            loader.dispose()
    except _RefusedValue as exc:
        kind = exc.node.tag.rsplit(":", 1)[-1]  # timestamp, int, float or bool
        # Marks count from 0 inside the YAML, whose first line is the file's second: the person's editor shows +2.
        # A key is any YAML string: only short plain text is named, never a line break, a lone surrogate ("\ud800",
        # which the JSON response can't encode) or an explicit `? key` of any length. Anything else gets its line.
        named = exc.field if exc.field and exc.field.isprintable() and len(exc.field) <= 80 else None
        where = named or f"The value on line {exc.node.start_mark.line + 2}"
        # The others' text is Python's. float()'s quotes the whole value, so it is cut too (int()'s stops at 200).
        reason = f": {str(exc.cause).rstrip('.')[:200]}" if isinstance(exc.cause, ValueError) else ""
        value = exc.node.value if len(exc.node.value) <= 80 else exc.node.value[:79] + "…"  # a 4301-digit int, say
        raise StoryError(f"{where} {value!r} isn't a valid {kind}{reason}.") from exc
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


def parse_when(value: Any, now: datetime, past_is_now: bool = False) -> datetime | None:
    """A schedule time, in UTC. Past (or < 2 minutes away) is an error, or None ("now") if ``past_is_now``;
    beyond SCHEDULE_HORIZON_DAYS is always an error."""
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
    try:
        when = when.astimezone(timezone.utc)
    except OverflowError as exc:  # e.g. 9999-12-31T23:00:00-05:00, which is in year 10000 in UTC
        raise StoryError(
            f"publish_at {when.isoformat()} is out of range; schedule it at most {SCHEDULE_HORIZON_DAYS} days ahead."
        ) from exc
    if when < now + timedelta(minutes=2):
        if past_is_now:
            return None
        raise StoryError(f"publish_at {when.isoformat()} is in the past or under 2 minutes away; omit it to publish now.")
    latest = now + timedelta(days=SCHEDULE_HORIZON_DAYS)
    if when > latest:
        raise StoryError(
            f"publish_at {when.isoformat()} is more than {SCHEDULE_HORIZON_DAYS} days ahead; the latest allowed is "
            f"{latest.isoformat(timespec='seconds')}."
        )
    return when


def _heading(match: re.Match) -> str:
    """A heading's text without its closing #s."""
    return match.group(1).rstrip().rstrip("#").strip()


def _strip_title_block(body: str, subtitle: str | None) -> tuple[str | None, str]:
    """Remove a leading `# Title` (returned) and a following `## Subtitle` that matches."""
    lines = body.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not (m := _H1.match(lines[i])):
        return None, "\n".join(lines[i:])
    h1 = _heading(m)
    rest = lines[i + 1:]
    j = 0
    while j < len(rest) and not rest[j].strip():
        j += 1
    if subtitle and j < len(rest) and (m2 := _H2.match(rest[j])) and _heading(m2).lower() == subtitle.lower():
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
    past_schedule_is_now: bool = False,
) -> Story:
    """Merge explicit arguments over front matter and validate the platform-neutral parts."""
    now = now or datetime.now(timezone.utc)
    known = list(known_platforms)
    if not markdown or not markdown.strip():
        raise StoryError("The markdown is empty. Read the file and pass its full contents.")

    meta, body = split_front_matter(markdown)
    # str(): YAML reads `title: 1984` as a number.
    subtitle = str(subtitle or _pick(meta, "subtitle") or "").strip() or None
    h1, body = _strip_title_block(body, subtitle)
    title = str(title or _pick(meta, "title") or h1 or "").strip()
    if len(title) < 2:
        raise StoryError("The story needs a title: pass `title`, add `title:` to the front matter, or start with `# Heading`.")

    canonical = canonical_url or _pick(meta, "canonical_url")
    cover = cover_image or _pick(meta, "cover_image")
    base = image_base_url or _pick(meta, "image_base_url")

    chosen = platforms or _as_tags(_pick(meta, "platforms")) or []
    chosen = list(dict.fromkeys(p.strip().lower() for p in chosen))  # once each: a repeat would post twice
    unknown = [p for p in chosen if p not in known]
    if unknown:
        raise StoryError(f"Unknown platform(s): {', '.join(unknown)}. Available: {', '.join(known)}.")

    overrides: dict[str, dict[str, Any]] = {}
    for pid in known:
        block = meta.get(pid)
        if isinstance(block, dict):
            overrides[pid] = dict(block)
            for key, value in block.items():  # the same rule as the top-level URLs
                if value and key in _ALIASES["canonical_url"] + _ALIASES["cover_image"]:
                    overrides[pid][key] = check_url(value, f"{pid}.{key}")
    for pid, dest in (destinations or {}).items():
        pid = pid.strip().lower()
        if pid not in known:
            raise StoryError(f"destinations has unknown platform {pid!r}. Available: {', '.join(known)}.")
        overrides.setdefault(pid, {})["destination"] = dest

    when_raw = publish_at or _pick(meta, "publish_at")
    warnings: list[str] = []
    # Git publishing re-reads files long after their date: there, a past publish_at means "now".
    when = parse_when(when_raw, now, past_is_now=past_schedule_is_now) if when_raw else None
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

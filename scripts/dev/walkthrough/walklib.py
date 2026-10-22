# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Helpers shared by the walkthrough scripts (walk.py, integrity_check.py, build_doc.py), with no browser and
no server behind them, so tests/test_walkthrough.py can cover them in milliseconds.

* masking of every credential a run creates, for screenshots, results.json and the document;
* the HTML of a panel: an MCP tool call or a REST request with its reply, plainly labelled as such;
* counting a results file; scanning output files for a credential that escaped the masking.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import pathlib
import re
from typing import Any

# Prefixes Inkcast gives its credentials: OAuth authorization codes, access and refresh tokens
# (services/identity/provider.py) and personal access tokens ``ink_pat_<id>_<secret>`` (libs/db/identity.py). The
# masked form keeps the prefix and the public id (the PAT's id, or the first characters of an OAuth value) and
# ends with an ellipsis.
PAT_RE = re.compile(r"ink_pat_([0-9a-f]{12})_[A-Za-z0-9_\-]+")
OAUTH_RE = re.compile(r"(ic_(?:at|rt|code)_)([A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+")
# What a leak looks like: a credential with enough of its secret part to be usable. The masked forms
# (``ink_pat_<id>…`` and ``ic_at_<6>…``) never match.
LEAK_RE = re.compile(rb"ink_pat_[0-9a-f]{12}_[A-Za-z0-9_\-]{8,}|ic_(?:at|rt|code)_[A-Za-z0-9_\-]{12,}")
ELLIPSIS = "…"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def mask(value: Any, literals: list[str] = ()) -> Any:
    """Every credential in ``value`` (a string, or a JSON-like structure, recursively) replaced by its masked form:
    the known shapes by regex, plus any literal in ``literals`` (a fake platform's fixed key, for instance). The
    run's password is never given here: it is typed into a form and echoed by nothing, so nothing has it to mask."""
    if isinstance(value, dict):
        return {k: mask(v, literals) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(v, literals) for v in value]
    if not isinstance(value, str):
        return value
    out = PAT_RE.sub(lambda m: f"ink_pat_{m.group(1)}{ELLIPSIS}", value)
    out = OAUTH_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{ELLIPSIS}", out)
    for s in literals:
        if s and s in out:
            out = out.replace(s, s[:4] + ELLIPSIS if len(s) > 8 else "***")
    return out


def pretty(value: Any, max_lines: int = 90) -> str:
    """JSON pretty-printed (strings as they are), cut after ``max_lines`` with a line saying how much was cut."""
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False, default=str)
    lines = text.splitlines() or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"({len(lines) - max_lines} more lines not shown)"]
    return "\n".join(lines)


PANEL_CSS = """
:root { color-scheme: light only; --ink:#1d1b18; --muted:#6b665d; --line:#e3ded3; --bg:#f7f5f0; --card:#fff; --ok:#1a7f37; --bad:#b42318; }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--ink); }
body { font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; padding: 24px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 22px 26px; }
.kind { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
h1 { font: 600 20px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 4px 0 2px; word-break: break-all; }
.verdict { float: right; font-weight: 700; font-size: 13px; padding: 3px 10px; border-radius: 999px; border: 1px solid; }
.verdict.ok { color: var(--ok); border-color: var(--ok); } .verdict.bad { color: var(--bad); border-color: var(--bad); }
.narration { color: var(--muted); margin: 0 0 14px; }
h2 { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 18px 0 6px; }
pre { margin: 0; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px;
      font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
footer { margin-top: 16px; font-size: 12px; color: var(--muted); }
"""


def panel_html(kind: str, title: str, call: str, reply: str, narration: str = "", ok: bool = True,
               footer: str = "") -> str:
    """A rendered record of one call: the label says what it is (an MCP tool call, a REST request); the call and
    the reply are shown verbatim in monospace. Everything is HTML-escaped. It is a document, not a client: no
    chat bubbles, no vendor's chrome."""
    e = html.escape
    verdict = "<span class='verdict ok'>ok</span>" if ok else "<span class='verdict bad'>FAILED</span>"
    narr = f"<p class='narration'>{e(narration)}</p>" if narration else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=color-scheme content=light>"
            f"<title>{e(title)}</title><style>{PANEL_CSS}</style></head><body><div class=card>"
            f"{verdict}<div class=kind>{e(kind)}</div><h1>{e(title)}</h1>{narr}"
            f"<h2>Call</h2><pre>{e(call)}</pre><h2>Reply</h2><pre>{e(reply)}</pre>"
            f"<footer>{e(footer)}</footer></div></body></html>")


def mcp_panel_title(tool: str) -> str:
    return f"MCP tool call — {tool}"


def rest_panel_title(method: str, path: str) -> str:
    return f"REST — {method.upper()} {path}"


def render_call(tool: str, arguments: dict) -> str:
    """``tool(arguments)`` the way a client logs it: the arguments pretty-printed, and a multi-line string argument
    (a markdown story) shown as a block after the call rather than as one escaped line."""
    inline, blocks = {}, []
    for k, v in arguments.items():
        if isinstance(v, str) and "\n" in v:
            inline[k] = f"<{k}: {len(v.splitlines())} lines, shown below>"
            blocks.append((k, v))
        else:
            inline[k] = v
    text = f"{tool}({pretty(inline)})" if inline else f"{tool}()"
    for k, v in blocks:
        text += f"\n\n{k} =\n{v.rstrip()}"
    return text


def counts(results: dict) -> dict[str, int]:
    """What a results.json holds: steps by verdict and by kind, images. Read, never typed."""
    steps = results.get("steps", [])
    by_kind: dict[str, int] = {}
    for s in steps:
        by_kind[s.get("kind", "?")] = by_kind.get(s.get("kind", "?"), 0) + 1
    return {
        "total": len(steps),
        "passed": sum(1 for s in steps if s.get("ok")),
        "failed": sum(1 for s in steps if not s.get("ok")),
        "screenshots": sum(1 for s in steps if s.get("kind") == "browser" and s.get("screenshot")),
        "panels": sum(1 for s in steps if s.get("kind") in ("mcp", "rest") and s.get("screenshot")),
        "mcp_calls": by_kind.get("mcp", 0),
        "rest_calls": by_kind.get("rest", 0),
        "browser_steps": by_kind.get("browser", 0),
    }


def leaks(data: bytes) -> list[str]:
    """Credentials found in full in ``data`` (their masked forms, for a report that itself cannot leak)."""
    return [mask(m.group(0).decode("ascii", "replace")) for m in LEAK_RE.finditer(data)]


def scan_dir(root: pathlib.Path, skip_suffixes: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Every file under ``root`` with a credential in full, mapped to the masked forms found."""
    found: dict[str, list[str]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.suffix.lower() in skip_suffixes:
            continue
        hits = leaks(path.read_bytes())
        if hits:
            found[str(path.relative_to(root))] = sorted(set(hits))
    return found


def pdf_page_count(data: bytes) -> int:
    """Pages in a PDF whose page objects are written in clear (Chromium's are): ``/Type /Page`` objects, not ``/Pages``."""
    return len(re.findall(rb"/Type\s*/Page(?![s/a-zA-Z])", data))

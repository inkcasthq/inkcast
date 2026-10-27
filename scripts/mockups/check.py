#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Check a design-canvas mockup the way docs/tooling/mockup-standard.md asks, and export it for the repo.

    scripts/mockups/check.py <Main.dc.html> --out <dir>                    # check every variant
    scripts/mockups/check.py <Main.dc.html> --out <dir> --export <dir>     # also export HTML + PNG per tab

The variants are every combination of the enum tweaks the artboard declares in data-props (theme, tab, range...).
Each is rendered with scripts/mockups/render.js and opened in headless Chromium at 390, 820 and 1440 px. A variant
fails on a leftover placeholder ({{, sc-, undefined, NaN), a page wider than the screen, or a background shorter
than the screen. --export writes one HTML file and one PNG per tab, in the theme --export-theme (default: light,
the repo's rule for screenshots), plus a phone-width PNG of the first tab.

Needs Node and Playwright with Chromium. Exits non-zero when any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

RENDER = Path(__file__).with_name("render.js")
WIDTHS = ((390, 844), (820, 1100), (1440, 900))
LEFTOVER = re.compile(r"\{\{|<sc-|\bundefined\b|\bNaN\b")


def enum_props(dc_html: str) -> dict[str, list[str]]:
    """The artboard's enum tweaks, from its data-props: {name: options}."""
    m = re.search(r"data-props='([^']*)'", dc_html)
    if not m:
        return {}
    props = json.loads(m.group(1).replace("&#39;", "'").replace("&amp;", "&"))
    return {k: v["options"] for k, v in props.items()
            if isinstance(v, dict) and v.get("editor") == "enum" and v.get("options")}


def render(src: Path, out: Path, props: dict[str, str]) -> None:
    subprocess.run(["node", str(RENDER), str(src), str(out), json.dumps(props)], check=True)


async def check_pages(pages: list[Path], shots: Path) -> list[str]:
    from playwright.async_api import async_playwright

    problems: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for width, height in WIDTHS:
            page = await browser.new_page(viewport={"width": width, "height": height})
            for f in pages:
                text = f.read_text()
                if LEFTOVER.search(re.sub(r"<style>[\s\S]*?</style>", "", text)):
                    problems.append(f"{f.name}: a placeholder was left in the page")
                await page.goto(f.resolve().as_uri())
                scroll_w, bg_h, view_h = await page.evaluate(
                    "[document.documentElement.scrollWidth,"
                    " document.body.firstElementChild.getBoundingClientRect().height, innerHeight]")
                if scroll_w > width:
                    problems.append(f"{f.name} at {width} px: the page is {scroll_w} px wide (scrolls sideways)")
                if bg_h < view_h:
                    problems.append(f"{f.name} at {width} px: the background stops at {bg_h:.0f} px of {view_h}")
                await page.screenshot(path=str(shots / f"{f.stem}-{width}.png"), full_page=True)
            await page.close()
        await browser.close()
    return problems


async def export(src: Path, tabs: list[str], theme: str, dest: Path) -> None:
    from playwright.async_api import async_playwright

    dest.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        desktop = await browser.new_page(viewport={"width": 1440, "height": 900})
        for tab in tabs:
            html = dest / f"{tab}.html"
            render(src, html, {"theme": theme, "tab": tab})
            await desktop.goto(html.resolve().as_uri())
            await desktop.screenshot(path=str(html.with_suffix(".png")), full_page=True)
        phone = await browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        await phone.goto((dest / f"{tabs[0]}.html").resolve().as_uri())
        await phone.screenshot(path=str(dest / f"{tabs[0]}-mobile.png"), full_page=True)
        await browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mockup", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="where rendered variants and screenshots go")
    ap.add_argument("--export", type=Path, help="write one HTML + PNG per tab here")
    ap.add_argument("--export-theme", default="light")
    args = ap.parse_args()

    enums = enum_props(args.mockup.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    names = list(enums)
    pages = []
    for combo in itertools.product(*(enums[n] for n in names)) if names else [()]:
        props = dict(zip(names, combo))
        out = args.out / f"{'-'.join(combo) or 'page'}.html"
        render(args.mockup, out, props)
        pages.append(out)
    problems = asyncio.run(check_pages(pages, args.out))
    for line in problems:
        print("FAIL", line)
    print(f"{len(pages)} variants x {len(WIDTHS)} widths checked; {len(problems)} problem(s)")
    if args.export and not problems:
        asyncio.run(export(args.mockup, enums.get("tab", ["page"]), args.export_theme, args.export))
        print(f"exported to {args.export}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

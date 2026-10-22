#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Build the walkthrough document from a run: results.json, integrity.jsonl, env.json and the pictures.

Every number in the document is read from those files or measured here (file sizes); nothing is typed in.
The pictures are embedded as downscaled JPEG data URIs (Chromium does the resizing, so no imaging library is
needed); a tall capture is cropped and its caption says so. Light theme only. Output, in the run directory:
walkthrough.html (self-contained), walkthrough.pdf (Chromium print) and summary.md (a few lines for the
ci-captures branch's README).

    build_doc.py <run dir> [<env.json>]

An optional <run dir>/findings.json, a list of {"title", "severity": "defect"|"note", "text"} written for that
run, becomes the Findings section; without it the section says none was recorded. The document is built from
whatever the run left, so a failed walk still gets its evidence on paper.
"""

from __future__ import annotations

import base64
import html
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from walklib import counts, mask, pdf_page_count, pretty  # noqa: E402

DOC_WIDTH = 1000      # px, the widest an image is embedded at
MAX_HEIGHT = 1300     # px after scaling: taller captures are cropped, and the caption says so

CSS = """
:root { color-scheme: light only; --ink:#1d1b18; --muted:#6b665d; --line:#e3ded3; --accent:#c2410c; --ok:#1a7f37; --bad:#b42318; --bg:#fff; --soft:#f7f5f0; }
* { box-sizing: border-box; }
body { font: 11pt/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: var(--ink); background: var(--bg); margin: 0; }
main { max-width: 1040px; margin: 0 auto; padding: 28px 36px 60px; }
h1 { font-size: 24pt; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 16pt; margin: 34px 0 8px; padding-top: 10px; border-top: 2px solid var(--line); page-break-after: avoid; }
h3 { font-size: 12.5pt; margin: 22px 0 6px; page-break-after: avoid; }
p { margin: 6px 0; }
.lede { color: var(--muted); font-size: 10.5pt; }
.outcome { font-size: 13pt; margin: 14px 0; padding: 12px 16px; border-left: 4px solid var(--ok); background: #f2f8f3; }
.outcome.bad { border-left-color: var(--bad); background: #fff5f4; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 9.5pt; }
th, td { border: 1px solid var(--line); padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: var(--soft); font-weight: 600; }
tr { page-break-inside: avoid; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 9pt; }
pre { background: var(--soft); border: 1px solid var(--line); padding: 8px 10px; overflow-x: auto; font-size: 8.5pt; white-space: pre-wrap; word-break: break-word; margin: 6px 0; }
figure { margin: 10px 0 16px; page-break-inside: avoid; }
figure img { max-width: 100%; border: 1px solid var(--line); border-radius: 4px; display: block; }
figure.panel img { max-width: 820px; }
figure.narrow img { max-width: 440px; }
figcaption { font-size: 9.5pt; color: var(--muted); margin-top: 5px; }
.ok { color: var(--ok); font-weight: 600; } .bad { color: var(--bad); font-weight: 600; }
.step { margin-top: 16px; }
.step p { page-break-after: avoid; }
.narration { margin: 2px 0 6px; }
.finding { border: 1px solid var(--line); border-left: 4px solid var(--bad); padding: 10px 14px; margin: 12px 0; background: #fff8f7; page-break-inside: avoid; }
.finding.note { border-left-color: #b7791f; background: #fffaf0; }
.small { font-size: 9.5pt; color: var(--muted); }
@page { size: A4; margin: 12mm 11mm; }
@media print { h2 { page-break-before: always; } h2.first, h2.flow { page-break-before: auto; } }
"""

RESIZE_JS = """
async ({data, width, maxHeight}) => {
  const img = new Image();
  await new Promise((ok, bad) => { img.onload = ok; img.onerror = bad; img.src = 'data:image/png;base64,' + data; });
  const scale = Math.min(1, width / img.naturalWidth);
  const w = Math.round(img.naturalWidth * scale);
  const fullH = Math.round(img.naturalHeight * scale);
  const h = Math.min(fullH, maxHeight);
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h);
  ctx.drawImage(img, 0, 0, img.naturalWidth, Math.round(h / scale), 0, 0, w, h);
  return {uri: c.toDataURL('image/jpeg', 0.86), cropped: fullH > maxHeight, width: w, height: h, sourceWidth: img.naturalWidth, sourceHeight: img.naturalHeight};
}
"""


def e(s) -> str:
    return html.escape("" if s is None else str(s))


def load(run: pathlib.Path):
    res = json.loads((run / "results.json").read_text())
    integrity = [json.loads(line) for line in (run / "integrity.jsonl").read_text().splitlines() if line.strip()] \
        if (run / "integrity.jsonl").exists() else []
    findings = json.loads((run / "findings.json").read_text()) if (run / "findings.json").exists() else []
    return res, integrity, findings


def key_fields(step: dict) -> str:
    """What the document shows of a reply next to its picture: the check it passed, and an abridged reply."""
    reply = step.get("reply")
    if reply is None:
        return ""
    if step.get("kind") == "rest" and isinstance(reply, dict):
        body = reply.get("body")
        shown = body if not isinstance(body, (dict, list)) else body
        text = f"HTTP {reply.get('status')}\n" + pretty(shown, max_lines=18)
    else:
        text = pretty(reply, max_lines=18)
    return f"<pre>{e(mask(text))}</pre>"


def build(run: pathlib.Path, env: dict) -> tuple[pathlib.Path, dict]:
    res, integrity, findings = load(run)
    c = counts(res)
    steps = res.get("steps", [])
    ok_all = c["failed"] == 0 and c["total"] > 0 and all(r.get("ok") for r in integrity) and bool(integrity)
    icount = {"passed": sum(1 for r in integrity if r.get("ok")), "total": len(integrity)}
    server = res.get("server") or {}
    parts: list[str] = []
    P = parts.append

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")

        def figure(name: str, caption: str, kind: str = "panel") -> str:
            path = run / name
            if not path.exists():
                return f"<p class='bad'>picture {e(name)} is missing</p>"
            data = base64.b64encode(path.read_bytes()).decode()
            r = page.evaluate(RESIZE_JS, {"data": data, "width": DOC_WIDTH, "maxHeight": MAX_HEIGHT})
            note = f" (shown to the first {r['height']} of {round(r['sourceHeight'] * r['width'] / r['sourceWidth'])} px; the full capture is {e(name)})" if r["cropped"] else ""
            cls = f" class='{kind}'"
            return (f"<figure{cls}><img src=\"{r['uri']}\" alt=\"{e(caption)}\" width='{r['width']}' height='{r['height']}'>"
                    f"<figcaption>{e(name)} — {e(caption)}{note}</figcaption></figure>")

        title = f"Inkcast end-to-end walkthrough — {e(env.get('inkcast_version', '?'))} at {e(str(env.get('git_sha', '?'))[:12])}"
        P(f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=color-scheme content=light><title>{title}</title>"
          f"<style>{CSS}</style></head><body><main>")
        P(f"<h1>{title}</h1>")
        P(f"<p class='lede'>Inkcast {e(env.get('inkcast_version'))} (commit {e(env.get('git_sha'))}, {e(env.get('git_ref'))}) run in development mode "
          f"against the fake DEV and Medium APIs, driven by a real MCP client (the Python SDK, OAuth with PKCE) and Playwright "
          f"(Chromium {e(env.get('chromium'))}, Playwright {e(env.get('playwright'))}, Python {e(env.get('python'))}) through "
          f"<span class='mono'>{e(res.get('base'))}</span>. Run {e(res.get('started'))} → {e(res.get('finished'))}. "
          f"Every number in this document is read from results.json, integrity.jsonl and env.json; nothing is typed in. "
          f"Generated credentials are masked everywhere; the password is never written.</p>")
        verdict = "PASS" if ok_all else "FAIL"
        P(f"<div class='outcome{'' if ok_all else ' bad'}'><b>Outcome: {verdict}.</b> {c['passed']} of {c['total']} steps passed "
          f"({c['mcp_calls']} MCP calls, {c['rest_calls']} REST requests, {c['browser_steps']} browser pages); "
          f"{icount['passed']} of {icount['total']} integrity checks passed.</div>")

        P("<h2 class='first'>1. What was measured</h2><table><tr><th>Measure</th><th>Value</th></tr>")
        for k, v in [
            ("Inkcast server (MCP initialize)", f"{server.get('name')} {server.get('version')}; {len(server.get('tools', []))} tools: {', '.join(server.get('tools', []))}"),
            ("MCP client", f"{res.get('client_name')} — dynamic registration, PKCE S256, redirect to {res.get('callback')}"),
            ("OAuth token", f"scope “{(res.get('oauth') or {}).get('scope')}”, expires in {(res.get('oauth') or {}).get('expires_in')} s"),
            ("Steps", f"{c['total']} ({c['passed']} passed, {c['failed']} failed): {c['mcp_calls']} MCP, {c['rest_calls']} REST, {c['browser_steps']} browser"),
            ("Pictures", f"{c['screenshots']} screenshots of Inkcast's pages, {c['panels']} rendered panels of MCP and REST calls"),
            ("Draft on DEV", f"“{(res.get('draft') or {}).get('title')}” → {(res.get('draft') or {}).get('url')} (post {(res.get('draft') or {}).get('post_id')})"),
            ("Scheduled then cancelled", f"“{(res.get('scheduled') or {}).get('title')}” for {(res.get('scheduled') or {}).get('publish_at')} (post {(res.get('scheduled') or {}).get('post_id')})"),
            ("Personal access token", f"{res.get('pat_prefix')} (masked; scope stories:sync)"),
            ("Git sync", f"{(res.get('sync') or {}).get('source')}:{(res.get('sync') or {}).get('path')} → "
                         f"{((res.get('sync') or {}).get('result') or {}).get('action')} {((res.get('sync') or {}).get('result') or {}).get('status')} "
                         f"{((res.get('sync') or {}).get('result') or {}).get('url')}"),
            ("Ledger entries", f"{len(res.get('ledger') or [])}"),
            ("Articles on the fake DEV at the end", f"{len(res.get('dev_articles') or [])}: " + "; ".join(
                f"“{a.get('title')}” ({'published' if a.get('published') else 'draft'})" for a in res.get("dev_articles") or [])),
            ("Integrity checks", f"{icount['passed']} of {icount['total']} passed"),
            ("Run", f"{res.get('started')} → {res.get('finished')}"),
        ]:
            P(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>")
        P("</table>")

        P("<h2>2. The journey, step by step</h2>")
        P("<p>Each step is pictured as it happened: a screenshot of the Inkcast page the person saw, or a panel rendering the "
          "MCP tool call or REST request the client made and the reply it got. The line under each picture is what the "
          "step checked; a check that fails fails the step. A page with a JavaScript error, an HTTP error status or a "
          "visible error is refused rather than pictured as a success.</p>")
        phase = None
        for s in steps:
            if s.get("phase") != phase:
                phase = s.get("phase")
                P(f"<h3>{e(phase)}</h3>")
            mark = "<span class='ok'>✓</span>" if s.get("ok") else "<span class='bad'>✗</span>"
            P(f"<div class='step'><p><b>Step {s.get('n')}</b> — {e(s.get('step'))} <span class='small'>({e(s.get('kind'))}, {e(s.get('t'))})</span></p>")
            if s.get("narration"):
                P(f"<p class='narration'>{e(s['narration'])}</p>")
            P(f"<p>{mark} {e(s.get('detail'))}</p>")
            if s.get("screenshot"):
                P(figure(s["screenshot"], s.get("detail", ""),
                         "narrow" if s.get("kind") == "browser" and not s.get("step", "").startswith("api") else
                         "wide" if s.get("kind") == "browser" else "panel"))
            if s.get("kind") in ("mcp", "rest") and s.get("reply") is not None and not s.get("screenshot"):
                P(key_fields(s))
            P("</div>")

        P("<h2>3. Integrity of what was left behind</h2>")
        P("<p>Checked from the files alone, after the servers were stopped (integrity_check.py): the article the fake DEV "
          "holds is the story the client published (title, and the sha256 of the body re-rendered through the server's "
          "own DEV renderer); the ledger's content hash equals the hash the server computes for the synced file; the "
          "cancelled schedule never reached DEV; no credential appears in full in any output file.</p>")
        if integrity:
            P("<table><tr><th>Check</th><th>Result</th><th>Expected</th><th>Actual</th><th>Note</th></tr>")
            for r in integrity:
                P(f"<tr><td>{e(r.get('check'))}</td><td class='{'ok' if r.get('ok') else 'bad'}'>{'pass' if r.get('ok') else 'FAIL'}</td>"
                  f"<td class='mono'>{e(pretty(r.get('expected'), 6))[:200]}</td><td class='mono'>{e(pretty(r.get('actual'), 6))[:200]}</td><td>{e(r.get('note'))}</td></tr>")
            P("</table>")
        else:
            P("<p class='bad'>integrity.jsonl is missing: the check did not run.</p>")

        P("<h2 class='flow'>4. Environment</h2><table><tr><th>Fact</th><th>Value</th></tr>")
        for k in ("git_sha", "git_ref", "inkcast_version", "python", "playwright", "mcp", "chromium", "os", "ci"):
            P(f"<tr><td>{e(k)}</td><td class='mono'>{e(env.get(k))}</td></tr>")
        P("</table>")
        P(f"<p class='small'>Browser pages at {res.get('viewport', {}).get('width')}×{res.get('viewport', {}).get('height')} CSS px, "
          f"device scale factor {res.get('viewport', {}).get('device_scale_factor')} (the API reference at "
          f"{(res.get('viewport', {}).get('api_docs') or {}).get('width')}×{(res.get('viewport', {}).get('api_docs') or {}).get('height')}); "
          "panels at 1000 CSS px wide. The pictures in this document are downscaled copies; the originals are next to it.</p>")

        P("<h2 class='flow'>5. Findings</h2>")
        if not findings:
            P("<p>No product finding was recorded for this run (no findings.json).</p>")
        for i, f in enumerate(findings, 1):
            cls = "finding" if f.get("severity", "defect") == "defect" else "finding note"
            P(f"<div class='{cls}'><b>Finding {i} — {e(f.get('title'))}.</b> {e(f.get('text'))}</div>")

        P("<h2 class='flow'>6. Files</h2><table><tr><th>File</th><th>Bytes</th><th>What</th></tr>")
        what = {".png": "picture", ".json": "data", ".jsonl": "data", ".log": "process log", ".html": "this document",
                ".pdf": "this document", ".md": "summary", ".txt": "text"}
        for f in sorted(p for p in run.iterdir() if p.is_file()):
            P(f"<tr><td class='mono'>{e(f.name)}</td><td>{f.stat().st_size:,}</td><td>{e(what.get(f.suffix, ''))}</td></tr>")
        P("</table></main></body></html>")

        html_path = run / "walkthrough.html"
        html_path.write_text("".join(parts))
        pdf_path = run / "walkthrough.pdf"
        page.goto(html_path.as_uri(), wait_until="load")
        page.emulate_media(media="print", color_scheme="light")
        page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True)
        browser.close()

    pages = pdf_page_count(pdf_path.read_bytes())
    summary = {"outcome": verdict, "steps": c, "integrity": icount, "pdf_pages": pages,
               "html_bytes": html_path.stat().st_size, "pdf_bytes": pdf_path.stat().st_size}
    (run / "summary.md").write_text(
        f"# Inkcast walkthrough — {verdict}\n\n"
        f"Inkcast {env.get('inkcast_version')} at `{str(env.get('git_sha'))[:12]}`, run {res.get('started')} → {res.get('finished')}. "
        f"{c['passed']} of {c['total']} steps passed ({c['mcp_calls']} MCP calls, {c['rest_calls']} REST requests, "
        f"{c['browser_steps']} browser pages); {icount['passed']} of {icount['total']} integrity checks passed.\n\n"
        f"The document: [walkthrough.html](walkthrough.html) · [walkthrough.pdf](walkthrough.pdf) ({pages} pages). "
        f"Data: results.json, integrity.jsonl, env.json.\n")
    return pdf_path, summary


def main() -> int:
    run = pathlib.Path(sys.argv[1])
    env_path = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else run / "env.json"
    env = json.loads(env_path.read_text()) if env_path.exists() else {}
    pdf_path, summary = build(run, env)
    print(f"{summary['outcome']}: html {summary['html_bytes']:,} bytes; pdf {summary['pdf_bytes']:,} bytes, {summary['pdf_pages']} pages -> {pdf_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

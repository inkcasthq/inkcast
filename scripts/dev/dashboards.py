#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Prove the operator dashboard against a live Prometheus and Grafana (deploy/dev, Compose or kind).

    scripts/dev/dashboards.py seed  --url http://localhost:8787 --password-file deploy/dev/.secrets/password
    scripts/dev/dashboards.py check --grafana http://localhost:3000 --prometheus http://localhost:9091 \
                                    --out deploy/dev/.captures

`seed` signs in the way an MCP client does (OAuth with PKCE, password sign-in, consent), connects the fake DEV
and Medium accounts, and makes real traffic: publishes, scheduled posts (waited for, so schedule lag has
samples), a Git sync, previews, and some refused requests (4xx). Nothing reaches a real platform.

`check` waits for Prometheus to scrape Inkcast, then runs every query of every panel through Grafana's own
query API, exactly as the dashboard does:
  - a query that errors fails the check (a typo, a metric renamed, a bad label);
  - the panels named by --require must return data (the ones the seed always feeds);
  - every other panel is reported as having data or not, never silently skipped.
It then takes a light-theme screenshot of the whole dashboard (Playwright) and writes `summary.md` and
`summary.json` next to it. Exit status is non-zero when any query errors or a required panel is empty.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2

REDIRECT = "http://localhost:53682/callback"
DASHBOARD_UID = "inkcast-platform"
REQUIRED = ["Pods scraped", "Version and plug-ins", "Scheduler ticks", "Publish outcomes", "Requests by status class",
            "Schedule lag p95", "Publish call p95"]


def story(title: str, platforms: str = "[devto, medium]", extra: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (f"---\ntitle: {title}\ntags: [python, inkcast]\ncanonical_url: https://blog.example.com/{slug}\n"
            f"platforms: {platforms}\n{extra}---\n\nA story made by the dashboard seed.\n")


# ---- seed -----------------------------------------------------------------------------------------------------------

def sign_in(c: httpx2.Client, base: str, password: str) -> str:
    """An OAuth access token, obtained the way an MCP client gets one: register, authorize, sign in, consent."""
    meta = c.get(f"{base}/.well-known/oauth-authorization-server").raise_for_status().json()
    client = c.post(meta["registration_endpoint"], json={
        "client_name": "inkcast dashboard seed", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).raise_for_status().json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(12)
    r = c.get(meta["authorization_endpoint"], params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": f"{base}/mcp",
        "scope": " ".join(meta.get("scopes_supported") or [])})
    while r.is_redirect and not r.headers["location"].startswith(REDIRECT):
        location = urljoin(str(r.url), r.headers["location"])
        if "/auth/consent" in location:
            page = c.get(location)
            fields = dict(re.findall(r'name=(\w+) value="([^"]*)"', page.text))
            r = c.post(urljoin(location, "/auth/consent"),
                       data={"pending": fields["pending"], "csrf": fields["csrf"], "decision": "approve"})
            continue
        r = c.get(location)
        if r.status_code == 200 and 'name=password' in r.text:            # the password sign-in page
            pending = re.search(r'name=pending value="([^"]*)"', r.text).group(1)
            r = c.post(urljoin(str(r.url), "/auth/callback"), data={"pending": pending, "password": password})
    if not (r.is_redirect and r.headers["location"].startswith(REDIRECT)):
        raise SystemExit(f"sign-in did not finish (HTTP {r.status_code}): {r.text[:300]}")
    query = parse_qs(urlparse(r.headers["location"]).query)
    if query.get("state", [""])[0] != state or "code" not in query:
        raise SystemExit(f"sign-in was refused: {query}")
    token = c.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "code_verifier": verifier, "resource": f"{base}/mcp"}).raise_for_status()
    return token.json()["access_token"]


def seed(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    password = pathlib.Path(args.password_file).read_text().strip()
    run = datetime.now(timezone.utc).strftime("%H%M%S")
    with httpx2.Client(timeout=30, follow_redirects=False) as c:
        for _ in range(60):                                                 # the server may still be starting
            try:
                if c.get(f"{base}/livez").status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            time.sleep(2)
        token = sign_in(c, base, password)
        h = {"Authorization": f"Bearer {token}"}
        for pid, fields in (("devto", {"api_key": "devto-key"}), ("medium", {"token": "medium-token"})):
            link = c.post(f"{base}/api/v1/platforms/{pid}/connect-link", headers=h).raise_for_status().json()
            c.post(link["url"], data=fields)
        published = 0
        for i in range(args.rounds):
            r = c.post(f"{base}/api/v1/posts", headers=h, json={"markdown": story(f"Seed {run} now {i}")})
            published += sum(x["status"] == "published" for x in r.raise_for_status().json()["results"])
            c.post(f"{base}/api/v1/preview", headers=h, json={"markdown": story(f"Seed {run} preview {i}")})
        # Two batches, 20 s apart: rate() sees no increase in a series' first sample, so if every scheduled send
        # landed in one scrape (when the lag histogram first appears) the schedule-lag panels would stay empty.
        first = (datetime.now(timezone.utc) + timedelta(seconds=args.schedule_in)).replace(microsecond=0)
        when = first.isoformat()
        scheduled = []
        for i in range(2):
            at = (first + timedelta(seconds=20 * i)).isoformat()
            r = c.post(f"{base}/api/v1/posts", headers=h, json={"markdown": story(f"Seed {run} later {i}"),
                                                                 "publish_at": at}).raise_for_status()
            scheduled += [x["post_id"] for x in r.json()["results"] if x.get("post_id")]
        files = [{"path": f"posts/seed-{run}-{i}.md", "markdown": story(f"Seed {run} git {i}", "[devto]")} for i in range(2)]
        c.post(f"{base}/api/v1/stories:sync", headers=h, json={"source": "github.com/acme/blog", "files": files})
        refused = [c.post(f"{base}/api/v1/posts", headers=h, json={"markdown": "x", "platforms": ["devto"]}).status_code,
                   c.post(f"{base}/api/v1/posts", headers=h, json={"markdown": story("Nowhere", "[myspace]")}).status_code,
                   c.get(f"{base}/api/v1/tokens").status_code]
        print(f"seeded: {published} published now, {len(scheduled)} scheduled for {when}, 2 synced, refused {refused}")
        deadline = time.time() + args.schedule_in + 140
        while scheduled and time.time() < deadline:                         # schedule lag needs real sends
            posts = c.get(f"{base}/api/v1/posts?days_back=1&days_ahead=1", headers=h).raise_for_status().json()
            status = {p["id"]: p["status"] for p in posts}
            if all(status.get(pid) not in (None, "scheduled", "publishing") for pid in scheduled):
                print("scheduled posts went out:", sorted(status[pid] for pid in scheduled))
                break
            time.sleep(3)
        else:
            if scheduled:
                print("warning: scheduled posts had not gone out in time", file=sys.stderr)
        summary = c.get(f"{base}/api/v1/insights/summary?days=1", headers=h)
        print("insights:", summary.status_code, json.dumps(summary.json().get("posts_by_status", {}))[:200])
    return 0


# ---- check ----------------------------------------------------------------------------------------------------------

def wait(url: str, ok, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx2.get(url, timeout=10)
            if ok(r):
                return
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx2.HTTPError as exc:
            last = str(exc)
        time.sleep(3)
    raise SystemExit(f"timed out waiting for {what}: {last}")


def panels(dashboard: dict) -> list[dict]:
    out = []
    for p in dashboard.get("panels", []):
        out.append(p)
        out.extend(p.get("panels", []))                                     # rows that hold panels
    return [p for p in out if p.get("targets")]


def has_values(frames: list[dict]) -> bool:
    for frame in frames:
        values = frame.get("data", {}).get("values", [])
        if len(values) > 1 and any(v is not None for v in values[-1]):
            return True
    return False


def check(args: argparse.Namespace) -> int:
    grafana, prom, ns = args.grafana.rstrip("/"), args.prometheus.rstrip("/"), args.namespace
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    query = f'inkcast_build_info{{namespace="{ns}"}}'
    wait(f"{prom}/api/v1/query?query={query}", lambda r: r.status_code == 200 and r.json()["data"]["result"],
         f"Prometheus to scrape Inkcast ({query})", args.timeout)
    wait(f"{grafana}/api/health", lambda r: r.status_code == 200, "Grafana", args.timeout)
    time.sleep(args.settle)                                                 # let rate() windows fill
    dashboard = httpx2.get(f"{grafana}/api/dashboards/uid/{DASHBOARD_UID}", timeout=20).raise_for_status().json()["dashboard"]
    rows, errors, empty_required = [], [], []
    for p in panels(dashboard):
        title, got_data, problems = p.get("title", "?"), False, []
        for t in p["targets"]:
            expr = t.get("expr", "").replace("$namespace", ns)
            r = httpx2.post(f"{grafana}/api/ds/query", timeout=30, json={
                "from": "now-30m", "to": "now", "queries": [{
                    "refId": "A", "datasource": {"type": "prometheus", "uid": "prometheus"}, "expr": expr,
                    "range": True, "intervalMs": 15000, "maxDataPoints": 200}]})
            result = r.json().get("results", {}).get("A", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200 or result.get("error"):
                problems.append(f"`{expr}`: {result.get('error') or r.text[:200]}")
            elif has_values(result.get("frames", [])):
                got_data = True
        required = any(title.startswith(req) for req in args.require)
        if problems:
            errors.extend(f"{title}: {x}" for x in problems)
        elif required and not got_data:
            empty_required.append(title)
        rows.append({"panel": title, "type": p.get("type"), "required": required, "data": got_data, "errors": problems})
    shot = out / "inkcast-platform.light.png"
    if not args.no_screenshot:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 1000}, color_scheme="light")
            page.goto(f"{grafana}/d/{DASHBOARD_UID}/?orgId=1&from=now-30m&to=now&theme=light&kiosk",
                      wait_until="networkidle", timeout=90_000)
            page.wait_for_timeout(4000)
            page.screenshot(path=str(shot), full_page=True)
            browser.close()
    ok = not errors and not empty_required
    lines = [f"# Dashboard check: {'PASS' if ok else 'FAIL'}", "",
             f"Grafana {grafana}, Prometheus {prom}, namespace `{ns}`, {len(rows)} panels.", "",
             "| Panel | Required | Data | Errors |", "|---|---|---|---|"]
    lines += [f"| {r['panel']} | {'yes' if r['required'] else ''} | {'yes' if r['data'] else 'no'} | "
              f"{'; '.join(r['errors']) or ''} |" for r in rows]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    (out / "summary.json").write_text(json.dumps({"ok": ok, "panels": rows}, indent=2) + "\n")
    print("\n".join(lines))
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    for t in empty_required:
        print(f"EMPTY (required): {t}", file=sys.stderr)
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--url", default="http://localhost:8787")
    s.add_argument("--password-file", default="deploy/dev/.secrets/password")
    s.add_argument("--rounds", type=int, default=3)
    s.add_argument("--schedule-in", type=int, default=130,
                   help="seconds until the scheduled posts are due (Inkcast refuses under 2 minutes)")
    c = sub.add_parser("check")
    c.add_argument("--grafana", default="http://localhost:3000")
    c.add_argument("--prometheus", default="http://localhost:9091")
    c.add_argument("--namespace", default="inkcast")
    c.add_argument("--out", default="deploy/dev/.captures")
    c.add_argument("--require", nargs="*", default=REQUIRED, help="panel titles (prefixes) that must have data")
    c.add_argument("--timeout", type=float, default=180)
    c.add_argument("--settle", type=float, default=30, help="seconds to wait after the first scrape")
    c.add_argument("--no-screenshot", action="store_true")
    args = parser.parse_args(argv)
    return seed(args) if args.cmd == "seed" else check(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

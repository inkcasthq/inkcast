# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""``inkcast sync``: publish markdown files from a repository, from any CI or a laptop; ``inkcast resend`` and
``inkcast cancel``: a person's decision on a post the sync reported as waiting for one.

    export INKCAST_URL=https://inkcast.example.com
    export INKCAST_TOKEN=ink_pat_...          # a personal access token with the stories:sync scope
    inkcast sync 'posts/**/*.md'              # publish new files, update changed ones, skip the rest
    inkcast sync --dry-run 'posts/**/*.md'    # say what would happen, with per-platform previews
    inkcast sync --removed-since <rev> 'posts/**/*.md'   # and cancel the pending posts of files removed since <rev>
    inkcast sync --fail-on-decision 'posts/**/*.md'      # exit 1 while a file's post waits for a person's decision
    inkcast resend <post-id>                  # send an unconfirmed post again as a new one (a posts:write token)
    inkcast cancel <post-id>                  # cancel a scheduled, recovering or unconfirmed post (a posts:write token)

The token is read from an environment variable only (never a command-line argument, which other
users of the machine could see). Files are sent in batches of 50, removed paths in batches of their
own; each batch carries an Idempotency-Key, so a retried batch can never publish twice. In GitHub
Actions the repository, commit and image URLs are detected automatically, results go to the job
summary, and the step outputs ``results`` (JSON) and ``urls``.

Exit codes: 0 all good, 1 something failed, 2 usage or configuration problem. ``inkcast sync`` exits 0 on a
file whose post waits for a person's decision (its last publish is ``unconfirmed``, or still ``recovering``):
only a person can resolve that, and CI must not fail on every push until they do unless the repository asks
for it with ``--fail-on-decision`` (#234). ``inkcast resend`` exits 1 when the new post is not published
(failed, unconfirmed or recovering in its turn: the reply says why).
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from typing import Any
from urllib.parse import quote, urlparse

import httpx2

BATCH = 50
ICONS = {"create": "+", "update": "~", "unchanged": "=", "stale": "!", "skipped": "-", "failed": "x", "removed": "D"}
MARKDOWN = (".md", ".markdown")
DECIDING = ("unconfirmed", "recovering")  # a 'skipped' result with one of these statuses waits for a person (#226)
CONFIRMED = ("published", "draft", "unlisted")


class CliError(Exception):
    """A usage or environment problem the command reports and exits 2 on."""


def _connection(prog: str, url: str, token_env: str) -> tuple[str, str] | int:
    """The server URL and the token every command needs, or the exit code (2) after saying what is missing. The token
    comes from the environment only; a personal access token; never sent over plain http but to this machine."""
    token = os.environ.get(token_env, "").strip()
    if not url or not token:
        print(f"{prog}: set INKCAST_URL (or --url) and {token_env}.", file=sys.stderr)
        return 2
    if not token.startswith("ink_pat_"):
        print(f"{prog}: {token_env} doesn't look like a personal access token (ink_pat_...).", file=sys.stderr)
        return 2
    url_host = urlparse(url).hostname or ""
    if not url.startswith("https://") and url_host not in ("localhost", "127.0.0.1", "::1"):
        print(f"{prog}: refusing to send the token over plain http to {url_host}; use an https INKCAST_URL.",
              file=sys.stderr)
        return 2
    return url, token


def _client(base_url: str, token: str) -> httpx2.Client:
    """The HTTP client every command talks to the server through (a test stands in a transport here)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "inkcast-sync"}
    return httpx2.Client(base_url=base_url.rstrip("/"), timeout=120, headers=headers)


def _detail(r: httpx2.Response) -> str:
    """What an error reply says: its ``message``, and the post it names (a retried resend's new post, #234)."""
    try:
        body = r.json()
        detail = body.get("message") or r.text
        return f"{detail} (post {body['post_id']})" if body.get("post_id") else detail
    except (ValueError, AttributeError):
        return r.text


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


_MAGIC = re.compile(r"[*?\[]")  # what makes a pattern component a wildcard for ``glob`` (its ``has_magic``)


def glob_match(pattern: str, path: str) -> bool:
    """Whether ``glob.glob(pattern, recursive=True)`` would list ``path`` if the file still existed: for the files a
    push removed, which ``collect`` can't see. ``path`` is relative and POSIX, as ``collect`` writes paths and git names
    them; ``pattern`` is as ``PurePosixPath`` writes it (no ``./``). The walk is glob's own, one component at a time,
    so it matches no more than glob does (#291): a component without a wildcard is a name as written, a hidden one
    too; one with a wildcard is ``fnmatch`` on one name, and never a hidden name (``.github``) unless it starts with
    ``.`` itself; ``**`` on its own is any number of directories, none hidden, and ``**`` among other characters is
    one ``*``. Python 3.13's ``glob.translate`` isn't used: 3.12 is supported, and it lets ``[!x]*`` match a hidden
    name where ``glob.glob`` doesn't."""
    return _globbed(pattern.split("/"), path.split("/"))


def _globbed(parts: list[str], names: list[str]) -> bool:
    if not parts:
        return not names
    head, rest = parts[0], parts[1:]
    if head == "**":
        if not rest:  # the last component: the file and any directories above it, never none (that is a directory)
            return bool(names) and not any(name.startswith(".") for name in names)
        for taken in range(len(names) + 1):  # zero or more directories; glob walks into no hidden one
            if _globbed(rest, names[taken:]):
                return True
            if taken < len(names) and names[taken].startswith("."):
                return False
        return False
    if not names:
        return False
    if _MAGIC.search(head):
        matched = (head.startswith(".") or not names[0].startswith(".")) and fnmatch.fnmatchcase(names[0], head)
    else:
        matched = names[0] == head
    return matched and _globbed(rest, names[1:])


def removed_since(rev: str, patterns: list[str]) -> list[str]:
    """The markdown files ``patterns`` would have collected that ``rev`` had and HEAD no longer has, in git's order:
    ``git diff --no-renames --diff-filter=DT``, so a renamed file is a removal (its new path is a new story), and so is
    a file that became a link or a submodule (``T``: ``collect`` lists it no more), relative to the current directory
    as the collected paths are, and regular files only, as ``collect`` lists them (a removed link or submodule, #417).
    Raises ``CliError`` when git can't tell (not a repository, or ``rev`` unknown: a shallow
    clone that never fetched it)."""
    if rev.startswith("-"):  # git would read it as an option (``--output=FILE`` writes a file), never a revision
        raise CliError(f"{rev!r} isn't a revision")
    try:
        diff = subprocess.run(["git", "diff", "--no-renames", "--diff-filter=DT", "--raw", "--relative", "-z",
                               rev, "HEAD"], capture_output=True)
    except OSError as exc:
        raise CliError(f"can't run git to list the files removed since {rev}: {exc}") from exc
    if diff.returncode != 0:
        said = diff.stderr.decode(errors="replace").strip()
        raise CliError(f"can't list the files removed since {rev}: {said or 'git diff failed'}")
    # As ``collect`` writes paths: no ``./``. A pattern ending in ``/`` or ``/.`` lists directories only, no file.
    globs = [str(pathlib.PurePosixPath(p)) for p in patterns if p.rsplit("/", 1)[-1] not in ("", ".")]
    found: list[str] = []
    entries = diff.stdout.split(b"\0")      # ``--raw -z``: ':<old mode> <new mode> <old> <new> D|T', then the path, each NUL-ended
    for meta, raw in zip(entries[::2], entries[1::2]):
        if meta.split()[0] not in (b":100644", b":100755"):  # a link (120000) or a submodule (160000) was never collected
            continue
        try:
            name = raw.decode()
        except UnicodeDecodeError:  # git names files in bytes; one that isn't UTF-8 was never synced (a request is text)
            continue
        path = str(pathlib.PurePosixPath(name)) if name else ""
        if path and pathlib.PurePosixPath(path).suffix.lower() in MARKDOWN and path not in found \
                and any(glob_match(g, path) or path == g for g in globs):  # ``collect`` falls back to a pattern naming a file
            found.append(path)
    return found


def detect_source() -> str:
    """``github.com/owner/repo``: from GitHub Actions, else the git remote."""
    if os.environ.get("GITHUB_REPOSITORY"):
        host = os.environ.get("GITHUB_SERVER_URL", "https://github.com").split("://", 1)[-1]
        return f"{host}/{os.environ['GITHUB_REPOSITORY']}"
    remote = _git("remote", "get-url", "origin")
    if remote.startswith("git@"):
        remote = remote[4:].replace(":", "/", 1)
    remote = remote.split("://", 1)[-1].split("@", 1)[-1]
    return remote[:-4] if remote.endswith(".git") else remote


def detect_ref() -> str | None:
    return os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD") or None


def raw_base(source: str, ref: str | None) -> str | None:
    """Where relative images in the repository can be fetched publicly (GitHub only)."""
    host, _, repo = source.partition("/")
    if not ref or host != "github.com" or repo.count("/") != 1:
        return None
    return f"https://raw.githubusercontent.com/{repo}/{ref}/"


def collect(patterns: list[str]) -> list[str]:
    """The markdown files ``patterns`` list, as git tracks them: regular files only, reached through no symbolic link,
    so each is one ``--removed-since`` can report later (#417). ``glob`` lists a directory named like a story (``x.md``)
    and walks into a linked directory, while git tracks a link as a link, with nothing of its own beneath it."""
    found: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True)) or ([pattern] if os.path.isfile(pattern) else [])
        for m in matches:
            p = pathlib.PurePosixPath(pathlib.Path(m).as_posix())
            if p.suffix.lower() in MARKDOWN and str(p) not in found and _regular_file(m):
                found.append(str(p))
    return found


def _regular_file(path: str) -> bool:
    """Whether ``path`` is a regular file with no symbolic link among the directories above it, as ``lstat`` sees each
    (a link to a file is a link). False too when it is gone since ``glob`` listed it."""
    parts = pathlib.Path(path).parts
    try:
        return not any(os.path.islink(os.path.join(*parts[:n])) for n in range(1, len(parts))) \
            and stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def build_requests(paths: list[str], source: str, ref: str | None, platforms: list[str] | None,
                   image_base: str | None, dry_run: bool, removed: list[str] = ()) -> list[dict[str, Any]]:
    """The file batches, then the removed paths in batches of their own (``files`` empty): a server from before
    ``removed`` refuses those with a 422 instead of silently keeping the posts."""
    batches = []
    for i in range(0, len(paths), BATCH):
        files = []
        for path in paths[i:i + BATCH]:
            directory = str(pathlib.PurePosixPath(path).parent)
            base = (image_base.rstrip("/") + "/" + ("" if directory == "." else directory + "/")) if image_base else None
            files.append({"path": path, "markdown": pathlib.Path(path).read_text(encoding="utf-8"),
                          "image_base_url": base})
        batches.append({"source": source, "ref": ref, "files": files, "platforms": platforms, "dry_run": dry_run})
    for i in range(0, len(removed), BATCH):
        batches.append({"source": source, "ref": ref, "files": [], "removed": list(removed[i:i + BATCH]),
                        "platforms": platforms, "dry_run": dry_run})
    return batches


def render_table(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        line = f"{ICONS.get(r['action'], '?')} {r['action']:<9} {r['platform']:<8} {r['path']}"
        if r.get("url"):
            line += f"  {r['url']}"
        if r.get("message"):
            line += f"\n    {r['message']}"
        lines.append(line)
    return "\n".join(lines)


def render_summary(results: list[dict[str, Any]], dry_run: bool) -> str:
    out = ["### Inkcast " + ("dry run" if dry_run else "sync"), "", "| | File | Platform | Result | Link |",
           "|---|---|---|---|---|"]
    for r in results:
        link = f"[open]({r['url']})" if r.get("url") else ""
        note = f" — {r['message']}" if r.get("message") else ""
        out.append(f"| {ICONS.get(r['action'], '?')} | `{r['path']}` | {r['platform']} | {r['action']}{note} | {link} |")
    return "\n".join(out) + "\n"


def sync_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="inkcast sync", description="Publish markdown files from a repository.")
    parser.add_argument("paths", nargs="+", help="files or globs, e.g. 'posts/**/*.md'")
    parser.add_argument("--url", default=os.environ.get("INKCAST_URL", ""), help="Inkcast URL (env INKCAST_URL)")
    parser.add_argument("--token-env", default="INKCAST_TOKEN", help="environment variable holding the token")
    parser.add_argument("--source", default=None, help="repository id; default: detected, e.g. github.com/acme/blog")
    parser.add_argument("--ref", default=None, help="commit being synced; default: detected")
    parser.add_argument("--platforms", default=None, help="comma-separated; overrides each file's front matter")
    parser.add_argument("--image-base-url", default=None,
                        help="public base URL of the repository root for relative images; default: raw GitHub URL")
    parser.add_argument("--dry-run", action="store_true", help="report and preview only; change nothing")
    parser.add_argument("--fail-on-stale", action="store_true", help="exit 1 if a changed file couldn't be updated")
    parser.add_argument("--fail-on-decision", action="store_true",
                        help="exit 1 if a file's post waits for a person's decision (its last publish is unconfirmed, "
                             "or still recovering): resend or cancel it (inkcast resend | cancel <post-id>)")
    parser.add_argument("--removed-since", metavar="REV", default=None,
                        help="also report the matching files removed between REV and HEAD (git diff), so their pending "
                             "posts are cancelled; the GitHub action passes the commit before the push")
    parser.add_argument("--json", action="store_true", help="print the raw results as JSON")
    args = parser.parse_args(argv)

    connection = _connection("inkcast sync", args.url, args.token_env)
    if isinstance(connection, int):
        return connection
    url, token = connection
    paths = collect(args.paths)
    removed: list[str] = []
    if args.removed_since:
        try:
            removed = removed_since(args.removed_since, args.paths)
        except CliError as exc:
            print(f"inkcast sync: {exc}", file=sys.stderr)
            return 2
    if not paths and not removed:
        print("inkcast sync: no markdown files matched" + (", and none were removed." if args.removed_since else "."),
              file=sys.stderr)
        return 0
    source = args.source or detect_source()
    if not source:
        print("inkcast sync: can't tell which repository this is; pass --source.", file=sys.stderr)
        return 2
    ref = args.ref or detect_ref()
    image_base = args.image_base_url or raw_base(source, ref)
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()] if args.platforms else None

    results: list[dict[str, Any]] = []
    with _client(url, token) as client:
        for body in build_requests(paths, source, ref, platforms, image_base, args.dry_run, removed):
            key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            try:
                r = client.post("/api/v1/stories:sync", json=body, headers={"Idempotency-Key": key})
            except httpx2.HTTPError as exc:
                print(f"inkcast sync: can't reach {url}: {exc}", file=sys.stderr)
                return 1
            if r.status_code != 200:
                print(f"inkcast sync: {r.status_code}: {_detail(r)}", file=sys.stderr)
                return 2 if r.status_code in (401, 403) else 1
            results += r.json()["results"]

    print(json.dumps(results, indent=2) if args.json else render_table(results))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(render_summary(results, args.dry_run))
    if os.environ.get("GITHUB_OUTPUT"):
        urls = " ".join(r["url"] for r in results if r.get("url"))
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"results={json.dumps(results, separators=(',', ':'))}\nurls={urls}\n")
    failed = any(r["action"] == "failed" for r in results)
    stale = any(r["action"] == "stale" for r in results)
    deciding = any(r["action"] == "skipped" and r.get("status") in DECIDING for r in results)
    return 1 if failed or (stale and args.fail_on_stale) or (deciding and args.fail_on_decision) else 0


def post_main(command: str, argv: list[str]) -> int:
    """``inkcast resend <post-id>`` and ``inkcast cancel <post-id>``: the person's decision on a post a sync reported
    as waiting for one (``POST /api/v1/posts/{id}/resend`` and ``.../cancel``, a ``posts:write`` token). The reply is
    the post: for a resend the new one (published, or failed, unconfirmed or recovering in its turn: exit 1, and the
    first post is cancelled either way); for a cancel the cancelled one, with its note when it may already be on the
    platform. The API's refusals (409: not unconfirmed, resent already, the platform not connected or not answering
    just now; 404) are exit 1 with the message; a token short of the scope is exit 2."""
    prog = f"inkcast {command}"
    parser = argparse.ArgumentParser(prog=prog, description={
        "resend": "Send an unconfirmed post again, now, as one new post; the old one is cancelled. Check the platform "
                  "first: the first send may have arrived.",
        "cancel": "Cancel a scheduled, recovering or unconfirmed post. A recovering or unconfirmed one may already be "
                  "on the platform; the reply says so."}[command])
    parser.add_argument("post_id", help="the post id, as the sync result or inkcast_posts names it (pst_...)")
    parser.add_argument("--url", default=os.environ.get("INKCAST_URL", ""), help="Inkcast URL (env INKCAST_URL)")
    parser.add_argument("--token-env", default="INKCAST_TOKEN", help="environment variable holding the token (posts:write)")
    parser.add_argument("--json", action="store_true", help="print the raw reply as JSON")
    args = parser.parse_args(argv)

    connection = _connection(prog, args.url, args.token_env)
    if isinstance(connection, int):
        return connection
    url, token = connection
    with _client(url, token) as client:
        try:
            # One path segment, whatever the id holds: "pst_x/cancel#" must not turn a resend into a cancel.
            r = client.post(f"/api/v1/posts/{quote(args.post_id, safe='')}/{command}")
        except httpx2.HTTPError as exc:
            print(f"{prog}: can't reach {url}: {exc}", file=sys.stderr)
            return 1
    if r.status_code != 200:
        print(f"{prog}: {r.status_code}: {_detail(r)}", file=sys.stderr)
        return 2 if r.status_code in (401, 403) else 1
    post = r.json()
    if args.json:
        print(json.dumps(post, indent=2))
    else:
        line = f"{post['status']}  " + (f"{post['url']}  " if post.get("url") else "") + f"(post {post['id']})"
        print(line + (f"\n    {post['error']}" if post.get("error") else ""))
        if command == "resend":
            print(f"Post {args.post_id} is cancelled; it may already be on the platform from the first send: check, and "
                  "remove the extra copy if it is.")
    return 0 if command == "cancel" or post["status"] in CONFIRMED else 1

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
own, and so are removed linked directories and submodules (#508); each batch carries an Idempotency-Key,
so a retried batch can never publish twice. In GitHub Actions the repository, commit and image URLs are
detected automatically, results go to the job summary, and the step outputs ``results`` (JSON) and ``urls``.

Exit codes: 0 all good, 1 something failed, 2 usage or configuration problem. ``inkcast sync`` exits 0 on a
file whose post waits for a person's decision (its last publish is ``unconfirmed``, or still ``recovering``):
only a person can resolve that, and CI must not fail on every push until they do unless the repository asks
for it with ``--fail-on-decision`` (#234). ``inkcast resend`` exits 1 when the new post is not published
(failed, unconfirmed or recovering in its turn: the reply says why).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
from typing import Any, NamedTuple
from urllib.parse import quote, urlparse

import httpx2

from .libs.globs import glob_match, glob_reaches_below

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


class Removals(NamedTuple):
    """What ``--removed-since`` reports, as the request names it."""

    files: list[str]  # ``removed``: markdown paths
    dirs: list[str]   # ``removed_dirs``: a deleted link without a markdown suffix, or a deleted submodule (#508)
    globs: list[str]  # ``removed_globs``: the patterns, as ``collect`` reads them, that the server matches below each


def removed_since(rev: str, patterns: list[str]) -> list[str]:
    """The markdown files ``patterns`` would have collected that ``rev`` had and HEAD no longer has: ``removals_since``'s
    ``files``."""
    return removals_since(rev, patterns).files


def removals_since(rev: str, patterns: list[str]) -> Removals:
    """What ``rev`` had and HEAD no longer has, in git's order: ``git diff --no-renames --diff-filter=DT``, relative to
    the current directory as the collected paths are.

    ``files`` are the markdown paths ``patterns`` would have collected: a renamed file is a removal (its new path is a
    new story), and so is a file that became a link or a submodule (``T``: ``collect`` lists it no more). A deleted link
    with a markdown suffix is a removal too: before #417 ``collect`` followed a link to a file and synced it as a story
    of its own (#476); a link that became a file is a new story.

    ``dirs`` are the deleted links without a markdown suffix and the deleted submodules (or ones that became something
    else) below which a pattern could list a path (#508). A release before #417 synced the stories of a linked directory
    under the link's path, and one before #476 those of a submodule, while git names only the one entry: what was synced
    below it is in the server's ledger, so the server cancels its entries of this source below each that a pattern
    matches. A link to a file and a link to a directory can't be told apart from the deleted entry, so the suffix
    decides. One that is a directory at HEAD is left out and named on stderr: its files are collected as stories, and a
    removal below it would cancel the posts this run makes.

    Raises ``CliError`` when git can't tell (not a repository, or ``rev`` unknown: a shallow clone that never fetched
    it)."""
    if rev.startswith("-"):  # git would read it as an option (``--output=FILE`` writes a file), never a revision
        raise CliError(f"{rev!r} isn't a revision")
    diff = _git_listing(f"the files removed since {rev}", "diff", "--no-renames", "--diff-filter=DT", "--raw",
                        "--relative", "-z", rev, "HEAD")
    # As ``collect`` writes paths: no ``./``. A pattern ending in ``/`` or ``/.`` lists directories only, no file.
    globs = [str(pathlib.PurePosixPath(p)) for p in patterns if p.rsplit("/", 1)[-1] not in ("", ".")]
    found: list[str] = []
    dirs: dict[str, str] = {}  # path: what it was
    entries = diff.split(b"\0")      # ``--raw -z``: ':<old mode> <new mode> <old> <new> D|T', then the path, each NUL-ended
    for meta, raw in zip(entries[::2], entries[1::2]):
        old, new = meta[1:].split()[:2]
        if old not in (b"100644", b"100755", b"120000", b"160000"):
            continue
        try:
            name = raw.decode()
        except UnicodeDecodeError:  # git names files in bytes; one that isn't UTF-8 was never synced (a request is text)
            continue
        path = str(pathlib.PurePosixPath(name)) if name else ""
        if not path:
            continue
        story = pathlib.PurePosixPath(path).suffix.lower() in MARKDOWN
        if old != b"160000" and (story or old != b"120000"):  # a file, or a link to one, at rev; none now
            if story and new not in (b"100644", b"100755") and path not in found \
                    and any(glob_match(g, path) or path == g for g in globs):  # ``collect`` falls back to a pattern naming a file
                found.append(path)
        elif path not in dirs and any(glob_reaches_below(g, path) for g in globs):
            dirs[path] = "symbolic link" if old == b"120000" else "submodule"
    if dirs:  # a tree at HEAD is a directory now; nothing else can hold a path below it
        listed = _git_listing("what HEAD holds", "--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", *dirs)
        for entry in listed.split(b"\0"):
            info, _, at = entry.partition(b"\t")
            now = os.fsdecode(at)
            if info.split(b" ")[1:2] == [b"tree"] and now in dirs:
                print(f"inkcast sync: not cancelled below {now}: it was a {dirs.pop(now)} and is a directory now; its "
                      "files are synced as stories, and a pending post an older release made there from a file it no "
                      "longer holds is cancelled by hand (inkcast cancel <post-id>)", file=sys.stderr)
    return Removals(found, list(dirs), globs)


def _git_listing(what: str, *args: str) -> bytes:
    """The output of ``git <args>``, or ``CliError`` naming ``what`` could not be listed."""
    try:
        run = subprocess.run(["git", *args], capture_output=True)
    except OSError as exc:
        raise CliError(f"can't run git to list {what}: {exc}") from exc
    if run.returncode != 0:
        said = run.stderr.decode(errors="replace").strip()
        command = next(arg for arg in args if not arg.startswith("-"))
        raise CliError(f"can't list {what}: {said or f'git {command} failed'}")
    return run.stdout


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
    """The markdown files ``patterns`` list, as git tracks them: regular files only, reached through no symbolic link
    and outside every submodule, so each is one ``--removed-since`` can report later (#417, #476). ``glob`` lists a
    directory named like a story (``x.md``) and walks into a linked directory or a submodule's working tree, while git
    tracks a link as a link and a submodule as one gitlink, with nothing of their own beneath them. Each markdown match
    skipped is named on stderr, once, with the reason."""
    found: list[str] = []
    skipped: list[str] = []
    submodules = _submodules()
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True)) or ([pattern] if os.path.isfile(pattern) else [])
        for m in matches:
            p = pathlib.PurePosixPath(pathlib.Path(m).as_posix())
            if p.suffix.lower() not in MARKDOWN or str(p) in found or str(p) in skipped:
                continue
            reason = _not_collected(m, *submodules)
            if reason:
                print(f"inkcast sync: skipped {p}: {reason}", file=sys.stderr)
                skipped.append(str(p))
            else:
                found.append(str(p))
    return found


def _submodules() -> tuple[str, list[str]]:
    """The repository's top directory and its submodules' paths from there: the index's gitlinks, mode 160000 in
    ``git ls-files --stage`` (gitformat-index(5): object type 1110). The top is the pathspec, since from a subdirectory
    git lists only the entries below it: an absolute path, taken literally even under ``GIT_LITERAL_PATHSPECS`` (``:/``
    is not), and no ``-C <top>``, which would read a relative ``GIT_DIR`` or ``GIT_WORK_TREE`` from there. Nothing
    outside a repository (a run started from above it): there is no index."""
    try:  # bytes, as git names paths: a top directory that isn't UTF-8 is still one
        top = os.fsdecode(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                         check=True).stdout.rstrip(b"\n"))
        staged = subprocess.run(["git", "ls-files", "--stage", "-z", "--full-name", "--", top], capture_output=True,
                                check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return "", []
    # ``-z``: '<mode> <object> <stage>\t<path>', each NUL-ended, the path verbatim (git-ls-files(1), OUTPUT)
    return top, [os.fsdecode(entry.split(b"\t", 1)[1]) for entry in staged.split(b"\0") if entry.startswith(b"160000 ")]


def _not_collected(path: str, top: str, submodules: list[str]) -> str | None:
    """Why ``collect`` doesn't list ``path``, or None when it does: a symbolic link among the directories above it or
    the path itself, as ``lstat`` sees each (a link to a file is a link); not a regular file (or gone since ``glob``
    listed it); or below one of the ``submodules`` of the repository at ``top``."""
    parts = pathlib.Path(path).parts
    try:
        for n in range(1, len(parts)):
            if os.path.islink(os.path.join(*parts[:n])):
                return f"below the symbolic link {pathlib.Path(*parts[:n]).as_posix()}"
        mode = os.lstat(path).st_mode
    except OSError:
        return "not a regular file"
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    if not stat.S_ISREG(mode):
        return "not a regular file"
    # getcwd, and so abspath, gives the physical path, as git gives the top (a link on the way is refused above)
    inner = pathlib.Path(os.path.relpath(os.path.abspath(path), top)).as_posix() if top else ""
    for submodule in submodules:
        if inner.startswith(submodule + "/"):
            return f"inside the submodule {submodule}"
    return None


def build_requests(paths: list[str], source: str, ref: str | None, platforms: list[str] | None,
                   image_base: str | None, dry_run: bool, removed: list[str] = (), removed_dirs: list[str] = (),
                   globs: list[str] = ()) -> list[dict[str, Any]]:
    """The file batches, then the removed paths in batches of their own (``files`` empty), then the removed directories
    with ``globs`` (``files`` empty, no ``removed``): a server from before ``removed``, or ``removed_dirs`` (#508),
    finds neither files nor removed paths and refuses the batch with a 422 instead of silently keeping the posts."""
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
    for i in range(0, len(removed_dirs), BATCH):
        for j in range(0, len(globs), BATCH):  # a request holds 50 of each; each batch pairs a share of both
            batches.append({"source": source, "ref": ref, "files": [], "removed_dirs": list(removed_dirs[i:i + BATCH]),
                            "removed_globs": list(globs[j:j + BATCH]), "platforms": platforms, "dry_run": dry_run})
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
                             "or still recovering): resend or cancel it (inkcast resend | cancel <post-id>); an old "
                             "entry with no post to resend or cancel waits for an edit of the file, published as a new post")
    parser.add_argument("--removed-since", metavar="REV", default=None,
                        help="also report the matching files removed between REV and HEAD (git diff), and the linked "
                             "directories and submodules removed, so the pending posts synced from them are cancelled; "
                             "the GitHub action passes the commit before the push")
    parser.add_argument("--json", action="store_true", help="print the raw results as JSON")
    args = parser.parse_args(argv)

    connection = _connection("inkcast sync", args.url, args.token_env)
    if isinstance(connection, int):
        return connection
    url, token = connection
    paths = collect(args.paths)
    removals = Removals([], [], [])
    if args.removed_since:
        try:
            removals = removals_since(args.removed_since, args.paths)
        except CliError as exc:
            print(f"inkcast sync: {exc}", file=sys.stderr)
            return 2
        # ``collect`` reads the working tree, not HEAD: a path a build step fills is synced, so neither removed (#535)
        # nor removed below (#508). For a relative pattern both lists are relative to the current directory, in
        # ``PurePosixPath``'s form; an absolute or ``..`` pattern reports no removal (``removals_since``).
        for p in [p for p in removals.files if p in paths]:
            removals.files.remove(p)
            print(f"inkcast sync: not cancelled {p}: this run syncs it", file=sys.stderr)
        for d in [d for d in removals.dirs if any(p.startswith(d + "/") for p in paths)]:
            removals.dirs.remove(d)
            print(f"inkcast sync: not cancelled below {d}: this run syncs files below it", file=sys.stderr)
    if not paths and not removals.files and not removals.dirs:
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
        for body in build_requests(paths, source, ref, platforms, image_base, args.dry_run, removals.files,
                                   removals.dirs, removals.globs):
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

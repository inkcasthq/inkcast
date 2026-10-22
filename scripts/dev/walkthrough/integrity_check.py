#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Independent checks on what the walk left behind, from the files alone, after the servers are gone.

1. The article on the fake DEV matches the published story: the title, and the sha256 of the body, both
   recomputed here from the story's markdown through the same DEV renderer the server uses
   (libs/platforms/devto.py, ``prepare``), not copied from the walk's reply.
2. The ledger's ``content_hash`` for the synced file equals the hash the server computes for it: the sha256 of the
   rendered DEV payload (services/publishing/sync.py, ``payload_hash``), recomputed here from the file.
3. The scheduled post that was cancelled never reached DEV: no article on the fake carries its title.
4. No credential the run created appears in full in any output file: personal access tokens (``ink_pat_…``),
   OAuth access and refresh tokens (``ic_at_…``, ``ic_rt_…``), in results.json, the panels' text, the logs, the
   document, everything under the run directory.
5. After the account deletion (E9.F9.5.S4): both of the person's tokens answered 401, the erasure was counted exactly
   once (``inkcast_identity_users_deleted_total``, ``via="return"`` for the visit back or ``via="event"`` when the
   sync's DELETE_ACCOUNT pass ran first), and the articles on the fake DEV are the same as before it: what was
   published stays.

Writes <run>/integrity.jsonl, one line per check, and exits 1 if any check fails.

    integrity_check.py <run dir>
    integrity_check.py <run dir> --leaks-only    # after the document is built: scan again, the document included,
                                                 # and append that row to integrity.jsonl
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

from walklib import scan_dir  # noqa: E402

from inkcast.libs.content.story import parse_story  # noqa: E402
from inkcast.libs.platforms.devto import DevtoPlatform  # noqa: E402
from inkcast.services.publishing.sync import payload_hash  # noqa: E402


def rendered_for_dev(markdown: str, *, draft: bool = False, for_sync: bool = False):
    """The DEV payload the server builds for this markdown, with no I/O (``prepare`` is pure)."""
    story = parse_story(markdown, known_platforms=["devto", "medium"], draft=draft, past_schedule_is_now=for_sync)
    return DevtoPlatform(http=None).prepare(story.for_platform("devto"), story.body)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def leak_row(run: pathlib.Path, name: str) -> dict:
    found = scan_dir(run, skip_suffixes=(".png", ".jpg", ".jpeg"))
    files = sum(1 for p in run.rglob("*") if p.is_file())
    return {"check": name, "ok": not found, "expected": {}, "actual": found,
            "note": f"{files} files scanned for ink_pat_/ic_at_/ic_rt_ values"}


def main() -> int:
    run = pathlib.Path(sys.argv[1])
    if "--leaks-only" in sys.argv[2:]:
        row = leak_row(run, "no credential in full in the document or any other output file")
        with (run / "integrity.jsonl").open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        print(f"  [{'ok ' if row['ok'] else 'FAIL'}] {row['check']}: {row['note']}" + ("" if row["ok"] else f" — {row['actual']}"))
        return 0 if row["ok"] else 1
    res = json.loads((run / "results.json").read_text())
    articles = res.get("dev_articles") or []
    by_title = {a.get("title"): a for a in articles}
    rows: list[dict] = []

    def check(name: str, ok: bool, expected, actual, note: str = "") -> None:
        rows.append({"check": name, "ok": bool(ok), "expected": expected, "actual": actual, "note": note})
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {note or ('matches' if ok else 'differs')}")

    # 1. The draft on DEV is the story the client published.
    draft = res.get("draft") or {}
    if draft.get("markdown"):
        want = rendered_for_dev(draft["markdown"], draft=True).payload["article"]
        got = by_title.get(want["title"]) or {}
        check("DEV article: title of the draft", got.get("title") == want["title"], want["title"], got.get("title"))
        check("DEV article: body of the draft (sha256)", got.get("body_markdown") is not None and sha(got["body_markdown"]) == sha(want["body_markdown"]),
              sha(want["body_markdown"]), sha(got["body_markdown"]) if got.get("body_markdown") else None,
              "the body DEV received is the body the renderer produces from the story")
        check("DEV article: the draft is unpublished", got.get("published") is False, False, got.get("published"))
    else:
        check("DEV article: title of the draft", False, "a draft in results.json", None, "the walk recorded no draft")

    # 2. The ledger's content hash is the server's hash of the synced file.
    sync = res.get("sync") or {}
    entries = [e for e in res.get("ledger") or [] if e.get("path") == sync.get("path") and e.get("platform") == "devto"]
    if sync.get("markdown") and entries:
        prepared = rendered_for_dev(sync["markdown"], for_sync=True)
        want = payload_hash(prepared)
        check("ledger: content_hash of the synced file", entries[0].get("content_hash") == want, want, entries[0].get("content_hash"),
              "sha256 of the rendered DEV payload, as services/publishing/sync.py computes it")
        check("ledger: the synced file is published", entries[0].get("status") == "published", "published", entries[0].get("status"))
        got = by_title.get(prepared.title) or {}
        check("DEV article: body of the synced file (sha256)", got.get("body_markdown") is not None
              and sha(got["body_markdown"]) == sha(prepared.payload["article"]["body_markdown"]),
              sha(prepared.payload["article"]["body_markdown"]), sha(got["body_markdown"]) if got.get("body_markdown") else None)
    else:
        check("ledger: content_hash of the synced file", False, "a ledger entry for the synced file", entries, "no sync or no entry recorded")

    # 3. The cancelled schedule never published.
    scheduled = res.get("scheduled") or {}
    title = scheduled.get("title")
    check("cancelled schedule: nothing on DEV", bool(title) and title not in by_title, "no article titled " + repr(title),
          [a.get("title") for a in articles], f"{len(articles)} article(s) on the fake DEV, none is the cancelled one" if title and title not in by_title else "")

    # 5. The account deletion: tokens dead, counted once, the platform's articles untouched.
    deletion = res.get("deletion") or {}
    check("deletion: the OAuth token and the PAT answer 401", deletion.get("me_after") == [401, 401], [401, 401], deletion.get("me_after"))
    metric = deletion.get("metric") or []
    check("deletion: counted exactly once (via=return, or via=event when the sync read the event first)",
          len(metric) == 1 and metric[0].endswith(" 1") and ('via="return"' in metric[0] or 'via="event"' in metric[0]),
          'inkcast_identity_users_deleted_total{via="return"|"event"} 1, one series', metric)
    after = [a.get("title") for a in res.get("dev_articles_after_deletion") or []]
    check("deletion: the articles on DEV outlived the account", bool(after) and after == [a.get("title") for a in articles],
          [a.get("title") for a in articles], after, f"{len(after)} article(s) on the fake DEV after the account is gone" if after else "")

    # 4. No credential in full in any output file (run_walk.sh scans once more, with --leaks-only, once the document exists).
    row = leak_row(run, "no credential in full in any output file")
    check(row["check"], row["ok"], row["expected"], row["actual"], row["note"])

    (run / "integrity.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    bad = sum(1 for r in rows if not r["ok"])
    print(f"{len(rows) - bad} / {len(rows)} checks pass -> {run / 'integrity.jsonl'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

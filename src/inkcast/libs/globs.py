# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Glob matching on paths that no longer exist, as ``glob.glob(pattern, recursive=True)`` would have listed them: for
``inkcast sync --removed-since`` (the files a push removed, which ``collect`` can't see) and for the Git sync, which
matches its ledger's paths below a removed linked directory or submodule against the same patterns (#508)."""

from __future__ import annotations

import fnmatch
import re

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
    return _walk(pattern.split("/"), path.split("/"), below=False)


def glob_reaches_below(pattern: str, directory: str) -> bool:
    """Whether ``glob_match(pattern, path)`` holds for some path below ``directory`` (``directory/...``), by the same
    walk: a pattern that can't list anything there has nothing to cancel there."""
    return _walk(pattern.split("/"), directory.split("/"), below=True)


def _walk(parts: list[str], names: list[str], below: bool) -> bool:
    """Whether ``parts`` take all of ``names`` (``below``: take them with components left for at least one more name).
    A table of whether ``parts[i:]`` take ``names[j:]``, filled from the end, each cell once: the server runs this on
    patterns a request names, and trying every split at each ``**`` grew as C(len(parts) + len(names), len(names)) (#508).
    ``**`` before other components is any number of names, none hidden: it takes none (``done[j]``), or takes one that
    isn't hidden and is again a ``**`` (``row[j + 1]``). A last ``**`` takes the name and any above it, never none (that
    is a directory), none hidden. Any other component takes one name (``_name``)."""
    n, last = len(names), len(parts) - 1
    clean = [True] * (n + 1)  # clean[j]: no hidden name among names[j:]
    for j in reversed(range(n)):
        clean[j] = clean[j + 1] and not names[j].startswith(".")
    done = [not below and j == n for j in range(n + 1)]  # no component left: it holds only with no name left
    for i in reversed(range(len(parts))):
        head = parts[i]
        row = [False] * (n + 1)
        row[n] = below or (head == "**" and i < last and done[n])  # names ran out with components left
        for j in reversed(range(n)):
            if head != "**":
                row[j] = done[j + 1] and _name(head, names[j])
            elif i == last:
                row[j] = clean[j]
            else:
                row[j] = done[j] or (not names[j].startswith(".") and row[j + 1])
        done = row
    return done[0]


def _name(head: str, name: str) -> bool:
    """Whether the pattern component ``head`` (not ``**``) lists the one name ``name``."""
    if _MAGIC.search(head):
        return (head.startswith(".") or not name.startswith(".")) and fnmatch.fnmatchcase(name, head)
    return name == head

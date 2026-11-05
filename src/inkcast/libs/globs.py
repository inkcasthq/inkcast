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
    return _globbed(pattern.split("/"), path.split("/"))


def glob_reaches_below(pattern: str, directory: str) -> bool:
    """Whether ``glob_match(pattern, path)`` holds for some path below ``directory`` (``directory/...``), by the same
    walk: a pattern that can't list anything there has nothing to cancel there."""
    return _below(pattern.split("/"), directory.split("/"))


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
    return bool(names) and _name(head, names[0]) and _globbed(rest, names[1:])


def _below(parts: list[str], names: list[str]) -> bool:
    """``_globbed``'s walk over the directories ``names``, holding when components are left for at least one more name."""
    if not names:
        return bool(parts)
    if not parts:
        return False
    head, rest = parts[0], parts[1:]
    if head == "**":
        if not rest:
            return not any(name.startswith(".") for name in names)
        for taken in range(len(names) + 1):  # at len(names): ``**`` took them all, and ``rest`` is left below
            if _below(rest, names[taken:]):
                return True
            if taken < len(names) and names[taken].startswith("."):
                return False
        return False
    return _name(head, names[0]) and _below(rest, names[1:])


def _name(head: str, name: str) -> bool:
    """Whether the pattern component ``head`` (not ``**``) lists the one name ``name``."""
    if _MAGIC.search(head):
        return (head.startswith(".") or not name.startswith(".")) and fnmatch.fnmatchcase(name, head)
    return name == head

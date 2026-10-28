# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The setup guide runs every Python command from the checkout's ``.venv`` (#301).

A bare ``python3`` is whatever the machine has, usually without Inkcast's dependencies: every test then fails at import,
which says nothing about the code. The guide's shell blocks name ``.venv/bin/…``; only creating the environment
(``python3 -m venv .venv``) runs outside it."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (document, heading of the section whose shell blocks are the setup guide). Other sections, such as README's hosted
# install, run commands on a cluster or in a container and are not checked.
GUIDE = (
    ("CONTRIBUTING.md", "## Workflow"),
    ("README.md", "## Quick start (local, single user)"),
    ("README.md", "## Development"),
    ("docs/handover.md", "## How the work is done"),
)
# A command word in command position: line start, or after &&, ||, ;, | or $(, past any NAME=value assignments
# (PYTHONPATH=src:tests python ... is as bare as python ...).
BARE = re.compile(r"(?:^|&&|\|\||;|\||\$\()\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(python3?|pip3?)(?=\s|$)")
CREATE_VENV = re.compile(r"^\s*python3? -m venv \.venv(\s|$)")


def shell_lines(document: str, heading: str) -> list[str]:
    """The lines of the fenced ``bash`` blocks in the section under ``heading``, comments removed."""
    text = (ROOT / document).read_text(encoding="utf-8")
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + 1)
    section = text[start:end if end != -1 else len(text)]
    blocks = re.findall(r"```bash\n(.*?)```", section, flags=re.S)
    return [line.split(" #", 1)[0] for block in blocks for line in block.splitlines() if line.strip()]


def bare_interpreters(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if not CREATE_VENV.match(line) and BARE.search(line)]


class TheSetupGuideUsesTheVenv(unittest.TestCase):
    def test_every_section_has_shell_blocks(self):
        for document, heading in GUIDE:
            with self.subTest(document=document, heading=heading):
                self.assertTrue(shell_lines(document, heading), "the section has no bash block: the check sees nothing")

    def test_no_python_or_pip_runs_outside_the_venv(self):
        for document, heading in GUIDE:
            with self.subTest(document=document, heading=heading):
                self.assertEqual(bare_interpreters(shell_lines(document, heading)), [])

    def test_the_check_finds_a_bare_interpreter(self):
        """The pattern itself: each way a bare interpreter appears is found, and the venv's own paths are not."""
        found = bare_interpreters([
            "python -m unittest", "  pip install -e .", "x && python3 scripts/gen-alerts.py",
            "K=$(python -c 'print(1)')", "cat f | python3 -c 'import sys'", "a; pip3 install ruff",
            "PYTHONPATH=src:tests python -m unittest discover -s tests",
        ])
        self.assertEqual(len(found), 7)
        self.assertEqual(bare_interpreters([
            "python3 -m venv .venv", ".venv/bin/pip install .", "PYTHONPATH=src .venv/bin/python -m unittest",
            "K=$(.venv/bin/python -c 'print(1)')", "scripts/dev/codeql.sh",
        ]), [])


if __name__ == "__main__":
    unittest.main()

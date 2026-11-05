# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #560 (issue #550): what scripts/ci-run.sh's annotation must also hold.

Each test runs the real script with bash, through the helpers of test_ci_run_cause_550, which read the annotation back
the way actions/runner v2.338.2 does.
"""

import pathlib
import subprocess
import tempfile
import unittest

from test_ci_run_cause_550 import LIMIT, SCRIPT, run, todays_tail, unescape_data

MARK = "----- cut here to fit the annotation; the step log has every line -----"


class CiRunReview550(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fails_with(self, data, status=1):
        path = pathlib.Path(self.tmp.name) / "output.bin"
        path.write_bytes(data)
        return run("x", "bash", "-c", f'cat "$1"; exit {status}', "_", str(path))

    def test_output_without_a_final_newline_still_gets_its_annotation(self):
        # The runner reads a workflow command only at the start of a line (ActionCommand.cs TryParseV2: TrimStart, then
        # StartsWith("::")). A last line with no newline would put "::error" in the middle of that line.
        for text in ("Error: the cause, and no newline", "".join(f"line {i}\n" for i in range(40)) + "50%"):
            with self.subTest(end=text[-3:]):
                status, out, annotation = self.fails_with(text.encode(), status=4)
                self.assertEqual(status, 4)
                self.assertIsNotNone(annotation, "no line of the output starts with ::error")
                self.assertTrue(out.startswith(text), "the step log still has the output")
                self.assertEqual(annotation[1], todays_tail(text + "\n"))

    def test_a_nul_byte_is_dropped_as_before(self):
        # Before #550, bash's command substitution dropped NUL bytes. mawk (CI's awk) prints them into the command line,
        # one-true-awk ends the line at one, and busybox awk splits it there.
        text = (b"error: above\x00the tail\n" + b"".join(b"line %d\n" % i for i in range(30))
                + b"before\x00after\n" + b"".join(b"end %d\n" % i for i in range(3)))
        _, out, (_, message) = self.fails_with(text)
        command = out[out.index("::error "):]
        self.assertNotIn("\x00", command)
        clean = text.replace(b"\x00", b"").decode()
        self.assertEqual(message, "error: abovethe tail\n----- the last 25 of 35 lines -----\n" + todays_tail(clean))

    def test_a_tail_that_fits_the_runner_is_kept_whole(self):
        # The runner keeps 4096 UTF-16 units (ExecutionContext.cs, issue.Message[.._maxIssueMessageLength]), not 4096
        # bytes. Playwright 1.56.0's progress lines (lib/server/registry/browserFetcher.js, getBasicDownloadProgress)
        # are 100 to 260 bytes but 100 to 102 units, so `playwright install`'s tail fit whole before #550.
        def bars(name):
            square, space = "\u25a0", " "
            yield f"Downloading {name} from https://cdn.playwright.dev/builds/{name}.zip"
            for row in range(11):
                yield f"|{square * (row * 8)}{space * ((10 - row) * 8)}| {str(row * 10).rjust(3)}% of 173.7 MiB"
            yield f"{name} downloaded to /home/runner/.cache/ms-playwright/{name}"
        outputs = {
            "playwright": "\n".join([*bars("chromium-1194"), *bars("chromium_headless_shell-1194"),
                                     "Error: Download failed: server returned code 503"]) + "\n",
            # 4025 units each, with every kind of lead byte: a character counted one unit too many cuts the tail
            "2-byte characters": ("\u00e9" * 160 + "\n") * 25,
            "3-byte characters": ("\u0800\u2500\ud000\uff21" * 40 + "\n") * 25,   # E0, E1-EC, ED, EE-EF
            "4-byte characters": ("\U0001f600\U000f0000\U00100000" * 26 + "\U0001f600" * 2 + "\n") * 25,   # F0, F3, F4
        }
        for name, text in outputs.items():
            with self.subTest(name):
                tail = todays_tail(text)
                self.assertGreater(len(tail.encode()), LIMIT)
                self.assertLessEqual(len(tail.encode("utf-16-le")) // 2, LIMIT)
                _, _, (_, message) = self.fails_with(text.encode())
                self.assertTrue(message.endswith(tail), "the whole tail, as before #550")
                self.assertLessEqual(len(message.encode("utf-16-le")) // 2, LIMIT)

    def test_a_message_never_holds_more_than_the_runner_keeps(self):
        # A character is 1 UTF-16 unit, or 2 for a 4-byte one, and each byte that is not part of a character becomes a
        # U+FFFD of 1 unit. Counting any of them as less would let the runner cut the message's last lines.
        outputs = {"2-byte characters": ("\u00e9" * 170 + "\n").encode() * 25,
                   "3-byte characters": ("\u0800\u2500\ud000\uff21" * 42 + "\n").encode() * 25,
                   "4-byte characters": ("\U0001f600\U000f0000\U00100000" * 30 + "\n").encode() * 25,
                   "stray continuation bytes": (b"\x80" * 200 + b"\n") * 25}
        for name, data in outputs.items():
            with self.subTest(name):
                path = pathlib.Path(self.tmp.name) / "output.bin"
                path.write_bytes(data)
                done = subprocess.run(["bash", str(SCRIPT), "x", "bash", "-c", 'cat "$1"; exit 1', "_", str(path)],
                                      capture_output=True, check=False)
                line = done.stdout[len(data):].decode("utf-8", errors="replace")   # as the runner decodes it
                message = unescape_data(line.rstrip("\n").split("::", 2)[2])
                self.assertIn(MARK, message)
                self.assertLessEqual(len(message.encode("utf-16-le")) // 2, LIMIT)

    def test_the_head_of_a_cut_never_splits_a_character(self):
        # The cut keeps the message's first and last bytes. Pad moves the first cut across every byte of a 2-, 3- and
        # 4-byte character, one byte at a time.
        for char in ("\u00e9", "\u20ac", "\U0001f600"):
            for pad in range(len(char.encode())):
                with self.subTest(char=char, pad=pad):
                    lines = ["a" * pad + char * 1500] + [f"{i:02} " + char * 400 for i in range(24)]
                    text = "".join(line + "\n" for line in lines)
                    _, _, (_, message) = self.fails_with(text.encode())   # run() decodes strictly
                    head, rest = message.split("\n" + MARK + "\n")
                    self.assertTrue(lines[0].startswith(head))
                    self.assertGreaterEqual(len(head.encode()), (LIMIT - len(MARK) - 2) // 2 - 3)
                    self.assertTrue(text.endswith(rest))
                    self.assertLessEqual(len(message.encode()), LIMIT)

    def test_the_causes_fill_the_room_to_the_byte_and_no_further(self):
        # The causes take only the room the tail and the separator leave: a cause that fits to the byte is shown, and one
        # a byte longer is not.
        tail = "".join(f"tail line {i:02} {'t' * 100}\n" for i in range(25))
        sep = "----- the last 25 of 26 lines -----\n"
        room = LIMIT - len(tail) - len(sep)
        for extra, shown in ((0, True), (1, False)):
            with self.subTest(extra=extra):
                cause = "error: " + "c" * (room - 1 - len("error: ") + extra)
                _, _, (_, message) = self.fails_with((cause + "\n" + tail).encode())
                self.assertEqual(message, cause + "\n" + sep + tail if shown else tail)
                self.assertLessEqual(len(message.encode()), LIMIT)


if __name__ == "__main__":
    unittest.main()

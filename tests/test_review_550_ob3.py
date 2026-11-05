# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of PR #560 (issue #550): what scripts/ci-run.sh's annotation must also hold.

Each test runs the real script with bash, through the helpers of test_ci_run_cause_550, which read the annotation back
the way actions/runner v2.338.2 does.
"""

import pathlib
import tempfile
import unittest

from test_ci_run_cause_550 import run, todays_tail


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


if __name__ == "__main__":
    unittest.main()

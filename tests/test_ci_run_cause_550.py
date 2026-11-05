# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""scripts/ci-run.sh's one error annotation shows why a command failed, wherever the tool prints it (issue #550).

The tests run the real script with bash on recorded or real output, and read the annotation back the way the runner
does (actions/runner v2.338.2, src/Runner.Common/ActionCommand.cs: UnescapeData and UnescapeProperty).
"""

import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci-run.sh"
LIMIT = 4096   # the runner keeps the first 4096 characters of an annotation's message (ExecutionContext.cs)

# The web job's install, `npm ci --ignore-scripts --no-audit --no-fund`, with typescript changed to 6.0.2 in
# web/package.json (issue #550). Recorded with npm 11.20.0, the version the issue measured, on Node 26.9.0. Two edits
# make it the output CI sees on the pinned Node 26.13.0: the five `npm warn EBADENGINE` lines, printed only because
# the local Node differs from the pin, are left out; and npm's five-line update notice, which it prints on CI just
# before the last line (npm 11.20.0 lib/cli/exit-handler.js), is put back. The result matches the issue's
# measurement: 103 lines, the cause on lines 3 and 5, and a 25-line tail that starts at `npm error   --dry-run`.
NPM_CI_OUTPUT = """\
npm error code EUSAGE
npm error
npm error `npm ci` can only install packages when your package.json and package-lock.json or npm-shrinkwrap.json are in sync. Please update your lock file with `npm install` before continuing.
npm error
npm error Invalid: lock file's typescript@6.0.3 does not satisfy typescript@6.0.2
npm error
npm error Clean install a project
npm error
npm error Usage:
npm error npm ci
npm error
npm error Options:
npm error [--install-strategy <hoisted|nested|shallow|linked>] [--legacy-bundling]
npm error [--global-style] [--omit <dev|optional|peer> [--omit <dev|optional|peer> ...]]
npm error [--include <prod|dev|optional|peer> [--include <prod|dev|optional|peer> ...]]
npm error [--strict-peer-deps] [--foreground-scripts] [--ignore-scripts]
npm error [--allow-directory <all|none|root>] [--allow-file <all|none|root>]
npm error [--allow-git <all|none|root>] [--allow-remote <all|none|root>]
npm error [--allow-scripts <package-list> [--allow-scripts <package-list> ...]]
npm error [--strict-allow-scripts] [--dangerously-allow-all-scripts] [--no-audit]
npm error [--no-bin-links] [--no-fund] [--dry-run]
npm error [-w|--workspace <workspace-name> [-w|--workspace <workspace-name> ...]]
npm error [--workspaces] [--include-workspace-root] [--install-links]
npm error
npm error   --install-strategy
npm error     Sets the strategy for installing packages in node_modules.
npm error
npm error   --legacy-bundling
npm error     Instead of hoisting package installs in `node_modules`, install packages
npm error
npm error   --global-style
npm error     Only install direct dependencies in the top level `node_modules`,
npm error
npm error   --omit
npm error     Dependency types to omit from the installation tree on disk.
npm error
npm error   --include
npm error     Option that allows for defining which types of dependencies to install.
npm error
npm error   --strict-peer-deps
npm error     If set to `true`, and `--legacy-peer-deps` is not set, then _any_
npm error
npm error   --foreground-scripts
npm error     Run all build scripts (ie, `preinstall`, `install`, and
npm error
npm error   --ignore-scripts
npm error     If true, npm does not run scripts specified in package.json files.
npm error
npm error   --allow-directory
npm error     Limits the ability for npm to install dependencies from directories.
npm error
npm error   --allow-file
npm error     Limits the ability for npm to install dependencies from tarball files.
npm error
npm error   --allow-git
npm error     Limits the ability for npm to fetch dependencies from git references.
npm error
npm error   --allow-remote
npm error     Limits the ability for npm to fetch dependencies from urls.
npm error
npm error   --allow-scripts
npm error     Comma-separated list of packages whose install-time lifecycle scripts
npm error
npm error   --strict-allow-scripts
npm error     If `true`, turn the install-script policy from a warning into a hard
npm error
npm error   --dangerously-allow-all-scripts
npm error     If `true`, bypass the `allowScripts` policy entirely and run every
npm error
npm error   --audit
npm error     When "true" submit audit reports alongside the current npm command to the
npm error
npm error   --bin-links
npm error     Tells npm to create symlinks (or `.cmd` shims on Windows) for package
npm error
npm error   --fund
npm error     When "true" displays the message at the end of each `npm install`
npm error
npm error   --dry-run
npm error     Indicates that you don't want npm to make any changes and that it should
npm error
npm error   -w|--workspace
npm error     Enable running a command in the context of the configured workspaces of the
npm error
npm error   --workspaces
npm error     Set to true to run the command in the context of **all** configured
npm error
npm error   --include-workspace-root
npm error     Include the workspace root when workspaces are enabled for a command.
npm error
npm error   --install-links
npm error     When set file: protocol dependencies will be packed and installed as
npm error
npm error
npm error aliases: clean-install, ic, install-clean, isntall-clean
npm error
npm error Run "npm help ci" for more info
npm notice
npm notice New major version of npm available! 11.20.0 -> 12.2.2
npm notice Changelog: https://github.com/npm/cli/releases/tag/v12.2.2
npm notice To update run: npm install -g npm@12.2.2
npm notice
npm error A complete log of this run can be found in: /root/.npm/_logs/2026-11-05T12_00_00_000Z-debug-0.log
"""


def unescape_data(value):
    return value.replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")


def unescape_property(value):
    return unescape_data(value.replace("%3A", ":").replace("%2C", ","))


def run(label, *command):
    """Run ci-run.sh; return its exit status, its whole stdout, and the annotation's (title, message) or None."""
    done = subprocess.run(["bash", str(SCRIPT), label, *command], capture_output=True, check=False)
    out = done.stdout.decode("utf-8")   # strict: a cut through a UTF-8 character fails here
    lines = [line for line in out.split("\n") if line.startswith("::error ")]
    if not lines:
        return done.returncode, out, None
    command_line = lines[-1]
    match = re.fullmatch(r"::error title=([^:]*)::(.*)", command_line)
    assert match, command_line
    assert re.fullmatch(r"(?:[^%\r\n]|%25|%0D|%0A)*", match[2]), "the message has a bad or half escape"
    return done.returncode, out, (unescape_property(match[1]), unescape_data(match[2]))


def todays_tail(text):
    """The annotation the script made before issue #550: the last 25 lines, each followed by a newline."""
    return "".join(line + "\n" for line in text.split("\n")[:-1][-25:])


class CiRunCause(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fails_with(self, text, status=1, label="x", encoding="utf-8"):
        """Run a command that prints `text` and exits with `status`, through ci-run.sh."""
        path = pathlib.Path(self.tmp.name) / "output.txt"
        path.write_bytes(text.encode(encoding))
        return run(label, "bash", "-c", f'cat "$1"; exit {status}', "_", str(path))

    def test_the_npm_ci_cause_on_lines_3_and_5_is_in_the_annotation(self):
        lines = NPM_CI_OUTPUT.split("\n")[:-1]
        self.assertEqual(len(lines), 103)
        self.assertIn("can only install packages when your package.json and package-lock.json", lines[2])
        self.assertTrue(lines[-25].startswith("npm error   --dry-run"))
        _, _, (title, message) = self.fails_with(NPM_CI_OUTPUT)
        self.assertEqual(title, "x failed (exit 1)")
        self.assertIn("can only install packages when your package.json and package-lock.json", message)
        self.assertIn("Invalid: lock file's typescript@6.0.3 does not satisfy typescript@6.0.2", message)
        self.assertLess(message.index("Invalid: lock file's"), message.index("npm error   --dry-run"))
        self.assertTrue(message.endswith(todays_tail(NPM_CI_OUTPUT)), "the tail is still there, last")
        self.assertLessEqual(len(message), LIMIT)

    def test_a_python_traceback_with_its_cause_at_the_end_shows_the_cause(self):
        program = textwrap.dedent("""\
            for i in range(80):
                print(f"step {i} ok")
            def deep(n):
                if n == 0:
                    raise ValueError("the cause at the end")
                deep(n - 1)
            deep(30)
            """)
        status, out, (_, message) = run("py", sys.executable, "-u", "-c", program)
        self.assertEqual(status, 1)
        self.assertTrue(out.split("::error ")[0].endswith("ValueError: the cause at the end\n"))
        self.assertIn("Traceback (most recent call last):", message)
        self.assertTrue(message.endswith("ValueError: the cause at the end\n"))

    def test_a_buffered_python_traceback_that_lands_first_is_shown(self):
        # Without -u, Python block-buffers stdout into a pipe: the traceback (stderr) comes out before the prints.
        # ci.yml sets PYTHONUNBUFFERED, which would turn the buffering off, so the child runs without it.
        program = 'for i in range(80):\n    print(f"step {i} ok")\nraise ValueError("the cause, printed first")\n'
        _, out, (_, message) = run("py", "env", "-u", "PYTHONUNBUFFERED", sys.executable, "-c", program)
        self.assertTrue(out.startswith("Traceback (most recent call last):"))
        self.assertIn("ValueError: the cause, printed first", message)
        self.assertTrue(message.endswith("step 79 ok\n"))

    def test_a_chained_traceback_shows_the_first_exception_above_the_tail(self):
        program = textwrap.dedent("""\
            def ping(n):
                if n == 0:
                    raise RuntimeError("could not publish the post")
                pong(n - 1)
            def pong(n):
                ping(n)
            try:
                {}["post-id"]
            except KeyError:
                ping(20)   # alternating frames, which the traceback can't fold into "repeated N more times"
            """)
        _, _, (_, message) = run("py", sys.executable, "-c", program)
        self.assertIn("KeyError: 'post-id'", message)
        self.assertTrue(message.endswith("RuntimeError: could not publish the post\n"))

    def test_unittest_failures_pushed_out_of_the_tail_are_shown(self):
        module = pathlib.Path(self.tmp.name) / "test_five.py"
        module.write_text("import unittest\n\nclass Five(unittest.TestCase):\n" + "".join(
            f"    def test_{n}(self):\n        self.assertEqual({n}, 0)\n\n" for n in range(1, 6)))
        status, _, (_, message) = run("tests", sys.executable, "-m", "unittest", "discover", "-s", self.tmp.name, "-v")
        self.assertEqual(status, 1)
        for n in range(1, 6):
            self.assertIn(f"FAIL: test_{n} ", message)
        self.assertTrue(message.endswith("FAILED (failures=5)\n"))

    def test_output_with_no_cause_lines_is_exactly_todays_tail(self):
        text = "".join(f"step {i} done: 100% of {i}\r\n" if i % 7 == 0 else f"step {i} done\n" for i in range(60))
        status, _, (title, message) = self.fails_with(text, status=3, label="lint")
        self.assertEqual((status, title), (3, "lint failed (exit 3)"))
        self.assertEqual(message, todays_tail(text))

    def test_short_output_is_annotated_whole_as_before(self):
        text = "error: pathspec 'nope' did not match any file(s) known to git\n"
        _, _, (_, message) = self.fails_with(text)
        self.assertEqual(message, text)

    def test_a_cause_in_the_tail_is_shown_once(self):
        text = "".join(f"line {i}\n" for i in range(40)) + "fatal: not a git repository\n" + "line end\n"
        _, _, (_, message) = self.fails_with(text)
        self.assertEqual(message, todays_tail(text))
        self.assertEqual(message.count("fatal: not a git repository"), 1)

    def test_at_most_15_cause_lines_come_before_the_tail(self):
        text = "".join(f"error: problem {i}\n" for i in range(40)) + "".join(f"line {i}\n" for i in range(30))
        _, _, (_, message) = self.fails_with(text)
        head, tail = message.split(todays_tail(text))
        self.assertEqual(tail, "")
        self.assertEqual(re.findall(r"error: problem (\d+)", head), [str(i) for i in range(15)])

    def test_the_causes_never_cost_the_tail_a_line(self):
        # This repo's own failing test run has tracebacks logged by passing tests above the tail, so the lines that
        # look like a cause can all be noise. They go in the room the tail leaves, never in place of the tail.
        noise = "".join(f"ValueError: logged by a passing test {i} {'x' * 200}\n" for i in range(15))
        tail = "".join(f"  File \"t.py\", line {i}, in test_{i}: {'y' * 70}\n" for i in range(24))
        text = noise + "".join(f"test_{i} ... ok\n" for i in range(100)) + tail + "FAILED (failures=1)\n"
        _, _, (_, message) = self.fails_with(text)
        self.assertTrue(message.endswith("----- the last 25 of 140 lines -----\n" + todays_tail(text)))
        self.assertTrue(message.startswith("ValueError: logged by a passing test 0 "))
        self.assertLessEqual(len(message), LIMIT)

    def test_a_log_line_that_names_an_error_is_not_a_cause(self):
        # Lines from this repo's test run: a warning that ends in an exception's name, and a test's name.
        text = ("2026-11-05 08:14:33,833 WARNING [Authz] inkcast.identity.sync: Keycloak gave no answer (ReadError)"
                " · event=identity.sync.failed error=OperationalError\n"
                "test_an_unexpected_error_is_counted (test_review_identity.KeycloakSyncKeepsGoing) ... ok\n"
                + "".join(f"line {i}\n" for i in range(30)))
        _, _, (_, message) = self.fails_with(text)
        self.assertEqual(message, todays_tail(text))

    def test_percent_cr_and_lf_survive_the_round_trip(self):
        text = "Error: 100% failed, %0A is not a newline\r\n" + "".join(f"line {i}\n" for i in range(30))
        _, out, (_, message) = self.fails_with(text)
        self.assertIn("Error: 100% failed, %0A is not a newline\r\n", message)
        self.assertIn("Error: 100%25 failed, %250A is not a newline%0D%0A", out)

    def test_the_title_is_escaped_as_a_property(self):
        # The toolkit's escapeProperty (actions/toolkit packages/core/src/command.ts) also escapes ':' and ','.
        _, out, (title, _) = self.fails_with("boom\n", label="stale, run: make 50%")
        self.assertEqual(title, "stale, run: make 50% failed (exit 1)")
        self.assertIn("::error title=stale%2C run%3A make 50%25 failed (exit 1)::", out)

    def test_a_tail_too_long_for_the_limit_is_cut_in_its_middle(self):
        wide = "é" * 99 + "😀"   # two-byte and four-byte characters, so a byte cut could split one
        for pad in range(4):     # moves both cuts across every byte of a character
            with self.subTest(pad=pad):
                x = "x" * pad
                text = ("npm error code E404\n" + "".join(f"{i:05} {wide}\n" for i in range(3000))
                        + "".join(f"Error: late {i}{x} {wide * 3}\n" for i in range(30)) + f"the last line{x}\n")
                status, out, (_, message) = self.fails_with(text, status=2)   # run() decodes strictly
                self.assertEqual(status, 2)
                self.assertLessEqual(len(message.encode("utf-8")), LIMIT)
                self.assertLessEqual(len(message.encode("utf-16-le")) // 2, LIMIT)   # what the runner counts
                self.assertGreater(len(message.encode("utf-8")), LIMIT - 8, "the budget is used, not wasted")
                self.assertTrue(message.startswith(f"Error: late 6{x} "), "the tail, with no room for causes")
                self.assertTrue(message.endswith(f"the last line{x}\n"))
                self.assertIn("\n----- cut here to fit the annotation; the step log has every line -----\n", message)
                self.assertIn(text, out, "the step log still has all of it")

    def test_the_exit_status_and_the_output_pass_through(self):
        status, out, annotation = run("ok", "bash", "-c", "echo fine; echo also >&2")
        self.assertEqual((status, annotation), (0, None))
        self.assertEqual(out, "fine\nalso\n")
        status, out, (title, message) = run("bad", "bash", "-c", "echo out; echo 'error: err' >&2; exit 42")
        self.assertEqual(status, 42)
        self.assertEqual(title, "bad failed (exit 42)")
        self.assertEqual(message, "out\nerror: err\n")
        self.assertTrue(out.startswith("out\nerror: err\n"))


if __name__ == "__main__":
    unittest.main()

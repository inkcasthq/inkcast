"""The walkthrough's own helpers (scripts/dev/walkthrough/walklib.py) and its CI wiring, without a browser or a
server: masking, the panel's HTML, the counts a document is built from, the credential scan, and the one
command's refusal to take a password on the command line."""

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WALK = ROOT / "scripts" / "dev" / "walkthrough"
sys.path.insert(0, str(WALK))

import walklib  # noqa: E402

PAT = "ink_pat_36ea8582899e_" + "Q" * 43
ACCESS = "ic_at_" + "a" * 43
REFRESH = "ic_rt_" + "b" * 43


class Masking(unittest.TestCase):
    def test_personal_access_token_keeps_its_id_and_loses_its_secret(self):
        self.assertEqual(walklib.mask(f"Bearer {PAT}"), "Bearer ink_pat_36ea8582899e…")

    def test_oauth_tokens_keep_six_characters(self):
        self.assertEqual(walklib.mask(f"{ACCESS} {REFRESH}"), "ic_at_aaaaaa… ic_rt_bbbbbb…")

    def test_literal_secrets_are_masked_too(self):
        self.assertEqual(walklib.mask("api-key: devto-key", ["devto-key"]), "api-key: devt…")
        self.assertEqual(walklib.mask("key devto", ["devto"]), "key ***")
        self.assertEqual(walklib.mask("nothing", [""]), "nothing")

    def test_masking_recurses_through_json_shapes(self):
        value = {"headers": {"Authorization": f"Bearer {PAT}"}, "list": [ACCESS, 3, None], "n": 1}
        self.assertEqual(walklib.mask(value), {"headers": {"Authorization": "Bearer ink_pat_36ea8582899e…"},
                                              "list": ["ic_at_aaaaaa…", 3, None], "n": 1})

    def test_the_masked_form_is_not_a_leak(self):
        for token in (PAT, ACCESS, REFRESH):
            self.assertEqual(walklib.leaks(walklib.mask(token).encode()), [])
            self.assertEqual(walklib.leaks(token.encode()), [walklib.mask(token)])


class Panels(unittest.TestCase):
    def test_everything_is_escaped(self):
        html = walklib.panel_html("MCP", "<script>alert(1)</script>", "call <b>", "reply & </pre><img onerror=x>",
                                  narration="<i>", footer="<u>")
        for raw in ("<script>", "<b>", "</pre><img", "<i>", "<u>"):
            self.assertNotIn(raw, html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("reply &amp; &lt;/pre&gt;", html)

    def test_the_panel_is_labelled_and_light_only(self):
        html = walklib.panel_html("Model Context Protocol, tools/call", walklib.mcp_panel_title("inkcast_posts"), "inkcast_posts()", "Posts:")
        self.assertIn("MCP tool call — inkcast_posts", html)
        self.assertIn("<meta name=color-scheme content=light>", html)
        self.assertNotIn("prefers-color-scheme", html)
        self.assertIn("verdict ok", html)
        self.assertIn("FAILED", walklib.panel_html("x", "y", "c", "r", ok=False))
        self.assertEqual(walklib.rest_panel_title("post", "/api/v1/stories:sync"), "REST — POST /api/v1/stories:sync")

    def test_a_multiline_argument_is_shown_as_a_block(self):
        text = walklib.render_call("inkcast_publish", {"markdown": "---\ntitle: T\n---\n\nbody\n", "draft": True})
        self.assertIn('"markdown": "<markdown: 5 lines, shown below>"', text)
        self.assertIn('"draft": true', text)
        self.assertTrue(text.endswith("markdown =\n---\ntitle: T\n---\n\nbody"))
        self.assertEqual(walklib.render_call("inkcast_platforms", {}), "inkcast_platforms()")

    def test_pretty_cuts_long_replies_and_says_so(self):
        text = walklib.pretty("\n".join(str(i) for i in range(100)), max_lines=10)
        self.assertEqual(text.splitlines()[-1], "(90 more lines not shown)")
        self.assertEqual(walklib.pretty({"a": 1}), '{\n  "a": 1\n}')


class Counting(unittest.TestCase):
    def test_counts_come_from_the_steps(self):
        results = {"steps": [
            {"kind": "rest", "ok": True, "screenshot": "01.png"}, {"kind": "mcp", "ok": True, "screenshot": "02.png"},
            {"kind": "browser", "ok": False, "screenshot": "03.png"}, {"kind": "check", "ok": True, "screenshot": None},
            {"kind": "browser", "ok": True, "screenshot": None}]}
        self.assertEqual(walklib.counts(results), {"total": 5, "passed": 4, "failed": 1, "screenshots": 1, "panels": 2,
                                                   "mcp_calls": 1, "rest_calls": 1, "browser_steps": 2})
        self.assertEqual(walklib.counts({})["total"], 0)

    def test_pdf_pages_are_page_objects_not_the_page_tree(self):
        pdf = b"%PDF-1.4\n1 0 obj << /Type /Pages /Kids [] >> endobj\n2 0 obj << /Type /Page >> endobj\n3 0 obj <</Type/Page>> endobj\n"
        self.assertEqual(walklib.pdf_page_count(pdf), 2)


class CredentialScan(unittest.TestCase):
    def test_scan_names_the_file_and_the_masked_value(self):
        with tempfile.TemporaryDirectory() as d:
            run = pathlib.Path(d)
            (run / "clean.json").write_text('{"token": "ink_pat_36ea8582899e…", "access": "ic_at_aaaaaa…"}')
            (run / "leaky.log").write_text(f"GET /x Authorization: Bearer {PAT}\n")
            (run / "shot.png").write_bytes(PAT.encode())  # pictures are not text: skipped by suffix
            self.assertEqual(walklib.scan_dir(run, skip_suffixes=(".png",)), {"leaky.log": ["ink_pat_36ea8582899e…"]})
            (run / "leaky.log").unlink()
            self.assertEqual(walklib.scan_dir(run, skip_suffixes=(".png",)), {})


class OneCommand(unittest.TestCase):
    """run_walk.sh takes --out only; the password is WALK_PASSWORD in the environment, never an argument."""

    def run_sh(self, *args):
        return subprocess.run(["bash", str(WALK / "run_walk.sh"), *args], capture_output=True, text=True, timeout=30)

    def test_a_password_argument_is_refused_before_anything_runs(self):
        r = self.run_sh("--out", "/nonexistent/x", "--password", "hunter22hunter22")
        self.assertEqual(r.returncode, 2)
        self.assertIn("WALK_PASSWORD in the environment", r.stderr)

    def test_out_is_required(self):
        r = self.run_sh()
        self.assertEqual(r.returncode, 2)
        self.assertIn("--out DIR is required", r.stderr)

    def test_walk_refuses_a_short_or_missing_password(self):
        r = subprocess.run([sys.executable, str(WALK / "walk.py"), "--out", "/nonexistent/x"], capture_output=True, text=True,
                           timeout=60, env={"PATH": "/usr/bin:/bin", "WALK_PASSWORD": "short", "PYTHONPATH": f"{ROOT / 'src'}"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("never an argument", r.stderr)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StartupEvidence(unittest.TestCase):
    """Issue #88: a crash before the first step still leaves results.json, with a failed ``startup`` step carrying the
    exception and the last lines of each process log, and build_doc.py builds a document from it instead of raising."""

    def build_doc(self, run: pathlib.Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(WALK / "build_doc.py"), str(run)], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'tests'}"})

    def test_a_server_that_never_starts_leaves_a_failed_startup_step_and_a_document(self):
        # The real server refuses to start: PORT outranks INKCAST_PORT in the config and the walk does not set it, so
        # the child dies with a config error while the walk polls a free port nobody listens on. The fakes get a free
        # port of their own; the metrics port 0 disables the metrics server; the callback port 0 is ephemeral.
        with tempfile.TemporaryDirectory() as d:
            run = pathlib.Path(d)
            env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'tests'}", "WALK_PASSWORD": "hunter22hunter22",
                   "WALK_INKCAST_PORT": str(free_port()), "WALK_FAKES_PORT": str(free_port()), "WALK_METRICS_PORT": "0",
                   "WALK_CALLBACK_PORT": "0", "PORT": "not-a-port"}
            r = subprocess.run([sys.executable, str(WALK / "walk.py"), "--out", str(run)], capture_output=True, text=True,
                               timeout=120, env=env)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertTrue((run / "results.json").exists(), "a crash at start-up must still leave results.json")
            res = json.loads((run / "results.json").read_text())
            self.assertEqual([s["step"] for s in res["steps"]], ["startup"])
            step = res["steps"][0]
            self.assertFalse(step["ok"])
            self.assertIn("RuntimeError: a server process exited", step["detail"])
            self.assertIn("not-a-port", step["logs"]["server.log"], "the server's last lines travel with the step")
            self.assertIn("fakes.log", step["logs"])
            self.assertNotIn("hunter22hunter22", (run / "results.json").read_text())
            self.assertEqual((res["total"], res["failed"]), (1, 1))
            self.assertIn("[FAIL] startup:", r.stdout)

            b = self.build_doc(run)
            self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
            html = (run / "walkthrough.html").read_text()
            self.assertIn("The walk failed at start-up; nothing else ran.", html)
            self.assertIn("The last lines of server.log", html)
            self.assertIn("not-a-port", html)
            self.assertTrue(b.stdout.startswith("FAIL:"), b.stdout)
            self.assertIn("# Inkcast walkthrough — FAIL", (run / "summary.md").read_text())

    def test_a_run_with_no_results_file_still_gets_a_document(self):
        with tempfile.TemporaryDirectory() as d:
            run = pathlib.Path(d)
            (run / "server.log").write_text("Traceback (most recent call last):\nRuntimeError: no\n")
            b = self.build_doc(run)
            self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
            html = (run / "walkthrough.html").read_text()
            self.assertIn("The walk failed at start-up; nothing else ran.", html)
            self.assertIn("results.json is missing", html)
            self.assertIn("0 of 0 steps passed", html)
            self.assertTrue((run / "walkthrough.pdf").exists())


class CallbackPort(unittest.TestCase):
    """The OAuth callback listens on a port the OS picks unless one is set (issue #114): a fixed port inside Linux's
    ephemeral range (32768-60999) can be held as the source port of any local connection, and the bind fails."""

    def test_the_default_is_a_free_port_the_os_picks(self):
        text = (WALK / "walk.py").read_text()
        self.assertIn('os.environ.get("WALK_CALLBACK_PORT", "0")', text)
        self.assertIn("self.port = self.server.server_address[1]", text)
        self.assertIn('facts["callback"] = callback.url', text)

    def test_the_listener_reports_the_port_it_got(self):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("walk_for_test", WALK / "walk.py")
            walk = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(walk)
        except ImportError as exc:   # walk.py needs Playwright and the MCP SDK; the walk job has them
            self.skipTest(f"walk.py's imports are not installed here: {exc}")
        callback = walk.Callback(0)
        try:
            self.assertNotEqual(callback.port, 0)
            self.assertEqual(callback.url, f"http://localhost:{callback.port}/callback")
            with socket.create_connection(("127.0.0.1", callback.port), timeout=5):
                pass
        finally:
            callback.server.shutdown()


class Wiring(unittest.TestCase):
    """Issue #57: the Python 3.14 test job runs the walkthrough and uploads it; on main the captures job publishes
    it to ci-captures next to the dashboards, and the branch README links the document."""

    def test_ci_runs_the_walkthrough_on_one_python_and_uploads_it(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("scripts/dev/walkthrough/run_walk.sh --out", ci)
        self.assertIn("name: walkthrough-${{ github.sha }}", ci)
        upload = ci.index("name: walkthrough-${{ github.sha }}")
        self.assertIn("if: always() && matrix.python == '3.14'", ci[upload - 400:upload])

    def test_publish_downloads_walkthrough_artifacts_too(self):
        publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
        self.assertIn("'dashboards-*'", publish)
        self.assertIn("'walkthrough-*'", publish)

    def test_the_branch_readme_links_the_document(self):
        script = (ROOT / "scripts" / "ci-publish-captures.sh").read_text()
        self.assertIn("walkthrough.html", script)
        self.assertIn("walkthrough.pdf", script)

    def test_every_walkthrough_script_has_the_licence_header(self):
        for path in sorted(p for p in WALK.iterdir() if p.is_file()):
            self.assertIn("LicenseRef-PolyForm-Shield-1.0.0", path.read_text()[:300], path.name)


if __name__ == "__main__":
    unittest.main()

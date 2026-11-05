# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""scripts/repo/settings.py when the organisation forbids Actions pull requests (issue #240): apply finishes every
other step, and check and apply name the organisation policy and the command that fixes it."""

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
REFUSED = "The organization does not allow GitHub Actions to create or approve pull requests"


def load_settings():
    spec = importlib.util.spec_from_file_location("repo_settings", REPO / "scripts" / "repo" / "settings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeGh:
    """Answers `gh api --include` as gh does: status line, headers, blank line and body on stdout; on an error,
    exit 1 and `gh: <message> (HTTP <status>)` on stderr. An unrouted call is a 404."""

    def __init__(self, routes: dict):
        self.routes, self.calls = routes, []

    def __call__(self, args, input=None, capture_output=True, text=True):
        method, path = args[3], args[4]
        self.calls.append((method, path))
        status, body = self.routes.get((method, path), (404, {"message": "Not Found"}))
        stdout = f"HTTP/2.0 {status} X\r\nContent-Type: application/json\r\n\r\n{'' if body is None else json.dumps(body)}"
        if status < 400:
            return subprocess.CompletedProcess(args, 0, stdout, "")
        return subprocess.CompletedProcess(args, 1, stdout, f"gh: {body['message']} (HTTP {status})")


def matching(owner_type: str = "Organization") -> dict:
    """Routes under which the live repository o/r matches the files exactly."""
    settings = json.loads((REPO / ".github" / "repo-settings.json").read_text())
    ruleset = json.loads((REPO / ".github" / "rulesets" / "main.json").read_text())
    live = {**settings["repository"], "owner": {"login": "o", "type": owner_type},
            "security_and_analysis": {k: {"status": v} for k, v in settings["security_and_analysis"].items()}}
    return {
        ("GET", "repos/o/r/rulesets?includes_parents=false"): (200, [{"name": ruleset["name"], "id": 7}]),
        ("GET", "repos/o/r/rulesets/7"): (200, {**ruleset, "id": 7}),
        ("GET", "repos/o/r"): (200, live),
        ("GET", "repos/o/r/vulnerability-alerts"): (204, None),
        ("GET", "repos/o/r/private-vulnerability-reporting"): (200, {"enabled": True}),
        ("GET", "repos/o/r/actions/permissions/workflow"): (200, settings["actions_workflow_permissions"]),
        ("GET", "orgs/o/actions/permissions/workflow"): (200, settings["actions_workflow_permissions"]),
    }


class OrganisationPolicy(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings()

    def run_with(self, routes: dict, fn, *args):
        fake = FakeGh(routes)
        with mock.patch.object(self.settings, "subprocess", types.SimpleNamespace(run=fake)):
            return fn(*args), fake.calls

    def test_apply_still_creates_the_ruleset_when_the_organisation_refuses(self):
        routes = {
            ("PATCH", "repos/o/r"): (200, {}),
            ("PUT", "repos/o/r/vulnerability-alerts"): (204, None),
            ("PUT", "repos/o/r/private-vulnerability-reporting"): (204, None),
            ("PUT", "repos/o/r/actions/permissions/workflow"): (409, {"message": REFUSED}),
            ("GET", "repos/o/r/rulesets?includes_parents=false"): (200, []),
            ("POST", "repos/o/r/rulesets"): (201, {"id": 7}),
        }
        with contextlib.redirect_stdout(io.StringIO()):
            failed, calls = self.run_with(routes, self.settings.apply, "o/r")
        self.assertIn(("POST", "repos/o/r/rulesets"), calls)
        self.assertEqual(len(failed), 1, failed)
        self.assertTrue(failed[0].startswith("actions workflow permissions: gh api PUT"), failed[0])
        self.assertIn("gh auth refresh -h github.com -s admin:org", failed[0])
        self.assertIn("gh api -X PUT orgs/o/actions/permissions/workflow", failed[0])

    def test_apply_lists_the_failure_and_exits_1(self):
        routes = {**matching(), ("PATCH", "repos/o/r"): (200, {}),
                  ("PUT", "repos/o/r/vulnerability-alerts"): (204, None),
                  ("PUT", "repos/o/r/private-vulnerability-reporting"): (204, None),
                  ("PUT", "repos/o/r/actions/permissions/workflow"): (409, {"message": REFUSED}),
                  ("PUT", "repos/o/r/rulesets/7"): (200, {"id": 7})}
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["settings.py", "apply", "o/r"]), contextlib.redirect_stdout(out):
            code, _ = self.run_with(routes, self.settings.main)
        self.assertEqual(code, 1)
        self.assertIn("FAILED: actions workflow permissions", out.getvalue())
        self.assertIn("1 apply step(s) failed", out.getvalue())

    def test_check_names_the_organisation_policy(self):
        routes = matching()
        routes[("GET", "repos/o/r/actions/permissions/workflow")] = (
            200, {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False})
        routes[("GET", "orgs/o/actions/permissions/workflow")] = (
            200, {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False})
        drift, _ = self.run_with(routes, self.settings.check, "o/r")
        self.assertEqual(len(drift), 2, drift)
        self.assertIn("can_approve_pull_request_reviews is False", drift[0])
        self.assertIn("the organisation o doesn't allow", drift[1])
        self.assertIn("-s admin:org", drift[1])

    def test_an_unreadable_organisation_policy_is_not_verified_not_drift(self):
        # GITHUB_TOKEN in the daily repo-settings workflow, or an owner login without admin:org.
        routes = matching()
        routes[("GET", "orgs/o/actions/permissions/workflow")] = (
            403, {"message": "You must be an org admin or have the actions policies fine-grained permission."})
        drift, _ = self.run_with(routes, self.settings.check, "o/r")
        self.assertEqual(drift, [])
        self.assertEqual(self.settings.UNVERIFIED, ["orgs/o/actions/permissions/workflow"])

    def test_a_personal_account_has_no_organisation_policy_to_read(self):
        drift, calls = self.run_with(matching(owner_type="User"), self.settings.check, "o/r")
        self.assertEqual(drift, [])
        self.assertNotIn(("GET", "orgs/o/actions/permissions/workflow"), calls)

    def test_a_failed_read_in_check_still_stops_with_the_error(self):
        routes = matching()
        del routes[("GET", "repos/o/r")]
        with mock.patch.object(sys, "argv", ["settings.py", "check", "o/r"]):
            with self.assertRaises(SystemExit) as stop:
                self.run_with(routes, self.settings.main)
        self.assertIn("gh api GET repos/o/r: HTTP 404", str(stop.exception.code))


def as_github_token(routes: dict) -> dict:
    """What GITHUB_TOKEN with contents: read gets (run 41852018655): every read answers 200, but the ruleset leaves out
    bypass_actors and the repository leaves out security_and_analysis and the merge settings; the admin-only paths
    answer 403 (#540)."""
    settings = json.loads((REPO / ".github" / "repo-settings.json").read_text())
    ruleset = {k: v for k, v in routes[("GET", "repos/o/r/rulesets/7")][1].items() if k != "bypass_actors"}
    live = {k: v for k, v in routes[("GET", "repos/o/r")][1].items()
            if k != "security_and_analysis" and k not in settings["repository"]}
    admin = (403, {"message": "Resource not accessible by integration"})
    return {**routes, ("GET", "repos/o/r/rulesets/7"): (200, ruleset), ("GET", "repos/o/r"): (200, live),
            ("GET", "repos/o/r/vulnerability-alerts"): admin, ("GET", "repos/o/r/actions/permissions/workflow"): admin,
            ("GET", "orgs/o/actions/permissions/workflow"): admin}


class OmittedFields(unittest.TestCase):
    """GitHub answers a token that can't see a setting with 200 and leaves the field out (#540): that is NOT VERIFIED,
    as a 403 is, and never a difference; a value it does return is still compared."""

    def setUp(self):
        self.settings = load_settings()

    def check(self, routes: dict) -> list[str]:
        with mock.patch.object(self.settings, "subprocess", types.SimpleNamespace(run=FakeGh(routes))):
            return self.settings.check("o/r")

    def test_the_scheduled_runs_token_finds_no_drift_when_the_settings_match(self):
        self.assertEqual(self.check(as_github_token(matching())), [])
        unverified = "\n".join(self.settings.UNVERIFIED)
        for name in ("bypass_actors", "security_and_analysis", "allow_squash_merge", "squash_merge_commit_message"):
            self.assertIn(name, unverified)

    def test_main_passes_and_lists_what_it_could_not_read(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["settings.py", "check", "o/r"]), contextlib.redirect_stdout(out), \
                mock.patch.object(self.settings, "subprocess", types.SimpleNamespace(run=FakeGh(as_github_token(matching())))):
            code = self.settings.main()
        self.assertEqual(code, 0, out.getvalue())
        self.assertNotIn("DRIFT", out.getvalue())
        self.assertIn("NOT VERIFIED (this token can't read it): repos/o/r/rulesets/7 bypass_actors", out.getvalue())

    def test_a_readable_rule_that_drifted_is_still_drift(self):
        routes = as_github_token(matching())
        ruleset = routes[("GET", "repos/o/r/rulesets/7")][1]
        routes[("GET", "repos/o/r/rulesets/7")] = (200, {**ruleset, "enforcement": "disabled"})
        drift = self.check(routes)
        self.assertEqual(len(drift), 1, drift)
        self.assertIn("enforcement", drift[0])

    def test_returned_values_that_differ_are_still_drift(self):
        routes = matching()
        live = routes[("GET", "repos/o/r")][1]
        ruleset = routes[("GET", "repos/o/r/rulesets/7")][1]
        routes[("GET", "repos/o/r")] = (200, {
            **live, "allow_merge_commit": True,
            "security_and_analysis": {**live["security_and_analysis"], "secret_scanning": {"status": "disabled"}}})
        routes[("GET", "repos/o/r/rulesets/7")] = (200, {**ruleset, "bypass_actors": [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]})
        drift = "\n".join(self.check(routes))
        for name in ("bypass_actors", "allow_merge_commit", "secret_scanning is 'disabled'"):
            self.assertIn(name, drift)
        self.assertEqual(self.settings.UNVERIFIED, [])

    def test_a_security_setting_missing_from_a_returned_object_is_drift(self):
        """An admin sees security_and_analysis; a setting missing from it is off or unavailable, not unreadable."""
        routes = matching()
        live = routes[("GET", "repos/o/r")][1]
        analysis = {k: v for k, v in live["security_and_analysis"].items() if k != "secret_scanning_non_provider_patterns"}
        routes[("GET", "repos/o/r")] = (200, {**live, "security_and_analysis": analysis})
        drift = self.check(routes)
        self.assertEqual(len(drift), 1, drift)
        self.assertIn("secret_scanning_non_provider_patterns is None", drift[0])


if __name__ == "__main__":
    unittest.main()

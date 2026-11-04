# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #478 (issue #436): scripts/ops/root-secret-drill.sh run end to end against a stub `oc`.

The stub keeps each object as a JSON file and answers every `-o jsonpath=` with the real kubectl's JSONPath
(`kubectl label --local --dry-run=client`, offline, an empty kubeconfig), so the drill's queries are judged by the
client the lab runs, not by a reading of them. No cluster is reached."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DRILL = ROOT / "scripts" / "ops" / "root-secret-drill.sh"
NS, ADP = "inkcast-staging", "openshift-adp"
VALUES = {"secret-key": "k" * 43, "fga-api-token": "t" * 32, "oidc-client-secret": "o" * 32}

# The stub `oc`. Objects live in $STUB_DIR/objects/<namespace>/<resource>/<name>.json; every call is logged.
# $STUB_SCENARIO: "pass" (everything works), "restore-fails" (the Restore ends Failed), "made-meanwhile" (a new
# inkcast-secrets with other values appears before the Restore runs, as OpenFGA's init container would make it).
STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, pathlib, subprocess, sys
    root = pathlib.Path(os.environ["STUB_DIR"])
    scenario = os.environ["STUB_SCENARIO"]
    args = sys.argv[1:]
    with open(root / "calls.log", "a") as log:
        log.write(json.dumps(args) + "\\n")
    ns = "default"
    if args[:1] == ["-n"]:
        ns, args = args[1], args[2:]
    KINDS = {"secret": "secret", "backups.velero.io": "backups.velero.io", "restores.velero.io": "restores.velero.io",
             "job": "job", "schedules.velero.io": "schedules.velero.io"}
    def path(namespace, resource, name):
        return root / "objects" / namespace / KINDS[resource] / f"{name}.json"
    def save(namespace, resource, obj):
        p = path(namespace, resource, obj["metadata"]["name"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))
    def target(rest):
        if "/" in rest[0]:
            resource, name = rest[0].split("/", 1)
            return resource, name, rest[1:]
        return rest[0], rest[1], rest[2:]
    verb = args[0]
    if verb == "get":
        resource, name, rest = target(args[1:])
        p = path(ns, resource, name)
        if not p.exists():
            sys.exit(f"Error from server (NotFound): {resource} {name} not found")
        output = rest[rest.index("-o") + 1]
        if output == "json":
            print(p.read_text())
        else:
            done = subprocess.run(["kubectl", "label", "--local", "-f", str(p), "stub=1", "--dry-run=client", "-o", output],
                                  capture_output=True, text=True, env={**os.environ, "KUBECONFIG": str(root / "kubeconfig")})
            sys.stdout.write(done.stdout); sys.stderr.write(done.stderr); sys.exit(done.returncode)
    elif verb == "create" and args[1] == "-f":
        obj = json.load(sys.stdin) if args[2] == "-" else json.loads(pathlib.Path(args[2]).read_text())
        kind = obj["kind"]
        namespace = obj["metadata"].get("namespace", ns)
        if kind == "Backup":
            obj["status"] = {"phase": "Completed", "progress": {"itemsBackedUp": 1}}
            save(namespace, "backups.velero.io", obj)
        elif kind == "Restore":
            target_ns = obj["spec"]["includedNamespaces"][0]
            if scenario == "made-meanwhile":
                save(target_ns, "secret", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "inkcast-secrets",
                     "namespace": target_ns, "labels": {"inkcast.io/root-secret": "true"}}, "data": {"secret-key": "bmV3"}})
            if scenario == "restore-fails":
                obj["status"] = {"phase": "Failed", "failureReason": "stubbed"}
            else:
                kept = root / "deleted.json"
                if not path(target_ns, "secret", "inkcast-secrets").exists():
                    save(target_ns, "secret", json.loads(kept.read_text()))
                    obj["status"] = {"phase": "Completed", "warnings": 0}
                else:
                    obj["status"] = {"phase": "Completed", "warnings": 1}
            save(namespace, "restores.velero.io", obj)
        elif kind == "Secret":
            p = path(namespace, "secret", obj["metadata"]["name"])
            if p.exists():
                sys.exit("Error from server (AlreadyExists)")
            for field in ("resourceVersion", "uid", "creationTimestamp"):
                if field in obj["metadata"]:
                    sys.exit(f"Error: metadata.{field} must not be set on create")
            save(namespace, "secret", obj)
        else:
            sys.exit(f"stub: cannot create {kind}")
    elif verb == "create" and args[1] == "job":
        name = args[-1]
        save(ns, "job", {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": ns},
                         "status": {"conditions": [{"type": "Complete", "status": "True"}]}})
    elif verb == "delete" and args[1] == "secret":
        p = path(ns, "secret", args[2])
        (root / "deleted.json").write_text(p.read_text())
        p.unlink()
        if scenario == "delete-drops":   # the server deleted it, the client's connection died before the answer
            sys.exit("error: unexpected EOF reading the server's response")
    elif verb == "exec":
        code_args = args[args.index("-c") + 2:]
        if code_args[0] == "seal":
            print("sealed-marker")
        else:
            print("marker: " + code_args[1]); print("2 of 2 sealed credentials read")
    elif verb == "logs":
        print(f"{ns}: every generated Secret is present and in agreement.")
    elif verb == "describe":
        print("stub describe")
    else:
        sys.exit(f"stub: unhandled {args}")
''')


@unittest.skipUnless(shutil.which("kubectl"), "the stub answers JSONPath with kubectl")
class DrillAgainstAStub(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="r478-drill-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "kubeconfig").write_text("")
        stub = self.dir / "oc"
        stub.write_text(STUB.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))   # this test's own interpreter
        stub.chmod(0o755)
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()
        secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": "inkcast-secrets", "namespace": NS, "resourceVersion": "41", "uid": "u-1",
                               "creationTimestamp": "2026-11-04T00:00:00Z",
                               "labels": {"app.kubernetes.io/part-of": "inkcast", "inkcast.io/root-secret": "true"}},
                  "data": {k: base64.b64encode(v.encode()).decode() for k, v in VALUES.items()}}
        self.save(NS, "secret", secret)
        self.save(ADP, "schedules.velero.io", {
            "apiVersion": "velero.io/v1", "kind": "Schedule",
            "metadata": {"name": "inkcast-staging-root-secret", "namespace": ADP, "labels": {"app.kubernetes.io/part-of": "inkcast"}},
            "spec": {"schedule": "47 3 * * *", "template": {"includedNamespaces": [NS], "includedResources": ["secrets"],
                                                            "storageLocation": "root-secrets", "ttl": "2160h0m0s"}}})

    def save(self, namespace: str, resource: str, obj: dict) -> None:
        path = self.dir / "objects" / namespace / resource / f"{obj['metadata']['name']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))

    def secret(self) -> dict | None:
        path = self.dir / "objects" / NS / "secret" / "inkcast-secrets.json"
        return json.loads(path.read_text()) if path.exists() else None

    def drill(self, scenario: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "KUBECTL": str(self.dir / "oc"), "STUB_DIR": str(self.dir), "STUB_SCENARIO": scenario,
               "TMPDIR": str(self.tmp), "INKCAST_PYTHON": sys.executable}
        done = subprocess.run(["bash", str(DRILL), NS], capture_output=True, text=True, env=env, timeout=120)
        for value in VALUES.values():
            self.assertNotIn(value, done.stdout + done.stderr, "no value is ever printed")
        return done

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.dir / "calls.log").read_text().splitlines()]

    def test_a_labelled_secret_passes_every_step(self):
        """The drill reads the label with a JSONPath the real client resolves: a key with dots in it is escaped
        (`inkcast\\.io/root-secret`); kubectl's `['inkcast.io/root-secret']` answers nothing, so the drill stopped
        at step 1 on every labelled Secret."""
        done = self.drill("pass")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Drill passed", done.stdout)
        self.assertIn("label inkcast.io/root-secret=true: present", done.stdout)
        self.assertEqual(sorted(VALUES), sorted(self.secret()["data"]))
        self.assertEqual(list(self.tmp.iterdir()), [], "the safety copy is removed when the drill passes")

    def test_a_failed_restore_puts_the_safety_copy_back(self):
        """The safety copy exists so that a failed restore is not a loss. When the drill stops after the delete
        with the Secret still absent, it puts the copy back (only the data, type, name and labels: a create refuses
        a resourceVersion) instead of removing the only copy with the rest of its directory."""
        done = self.drill("restore-fails")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(["-n", NS, "delete", "secret", "inkcast-secrets"], self.calls(), "the drill reached the loss")
        back = self.secret()
        self.assertIsNotNone(back, "inkcast-secrets is absent and its safety copy was removed:\n" + done.stderr)
        self.assertEqual(back["data"], {k: base64.b64encode(v.encode()).decode() for k, v in VALUES.items()})
        self.assertEqual(back["metadata"]["labels"]["inkcast.io/root-secret"], "true")
        self.assertIn("put back from the safety copy", done.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [], "once put back, the copy is removed")

    def test_a_delete_whose_answer_never_arrives_still_puts_the_copy_back(self):
        """The server can delete the Secret and the client still exit non-zero (its connection dropped before the
        answer). `set -e` then ends the drill on that command, so the mark `finish` reads must be set before the
        delete, not after it: set after, the mark is never reached, `finish` removes the only copy, and the Secret
        is gone for good."""
        done = self.drill("delete-drops")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(["-n", NS, "delete", "secret", "inkcast-secrets"], self.calls(), "the drill reached the loss")
        back = self.secret()
        self.assertIsNotNone(back, "inkcast-secrets is absent and its safety copy was removed:\n" + done.stderr)
        self.assertEqual(back["data"], {k: base64.b64encode(v.encode()).decode() for k, v in VALUES.items()})
        self.assertIn("put back from the safety copy", done.stderr)

    def test_a_secret_made_meanwhile_keeps_the_safety_copy(self):
        """OpenFGA's init container makes a new inkcast-secrets when its pod starts while the Secret is absent;
        the Restore (existingResourcePolicy none) then keeps the new one, and step 5 finds other values. The copy
        of the values the sealed credentials need is then kept, mode 600, and the drill says where and how."""
        done = self.drill("made-meanwhile")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("the restored Secret differs", done.stderr)
        (kept,) = list(self.tmp.iterdir()) or [None]
        self.assertIsNotNone(kept, "the only copy of the original values was removed:\n" + done.stderr)
        self.assertEqual(kept.stat().st_mode & 0o777, 0o700)
        self.assertEqual((kept / "put-back.json").stat().st_mode & 0o777, 0o600)
        self.assertIn(str(kept / "put-back.json"), done.stderr, "the drill names the copy and the command")
        self.assertIn("replace", done.stderr)


class RunbookBlocks(unittest.TestCase):
    def test_no_fenced_block_of_the_runbook_is_empty(self):
        """Step 1's commands are the ones the drill runs: a fenced block with no command in it is a command lost."""
        text = (ROOT / "docs" / "runbooks" / "env-secrets.md").read_text()
        blocks = re.findall(r"^ *```[a-z]*\n(.*?)^ *```$", text, flags=re.M | re.S)
        self.assertTrue(blocks)
        self.assertEqual([b for b in blocks if not b.strip()], [], "an empty ```bash block in docs/runbooks/env-secrets.md")


if __name__ == "__main__":
    unittest.main()

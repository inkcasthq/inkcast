# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #423 (issue #342): the CronJob inkcast-env-secrets as both environments render it, field by field.

tests/test_env_secrets_manifests.py checks the fields the CronJob sets, but not that it sets nothing else, so a
`suspend: true` (no tick ever runs, while `kubectl create job --from=cronjob`, the e2e's proof 6, still works),
a `parallelism` or `completions` above one (several runs at once, the race Forbid is there to avoid), a
`hostNetwork: true` or a `priorityClassName` all left every test green. Each field set is pinned here."""

from __future__ import annotations

import shutil
import unittest

from test_openfga_component import one, render

ENVIRONMENTS = ("environments/staging", "environments/production", "overlays/ci-env-secrets")
SPEC = {"schedule", "timeZone", "concurrencyPolicy", "startingDeadlineSeconds", "successfulJobsHistoryLimit",
        "failedJobsHistoryLimit", "jobTemplate"}
JOB = {"backoffLimit", "activeDeadlineSeconds", "template"}
POD = {"restartPolicy", "serviceAccountName", "automountServiceAccountToken", "enableServiceLinks", "imagePullSecrets",
       "securityContext", "containers", "volumes"}
CONTAINER = {"name", "image", "args", "resources", "securityContext", "volumeMounts"}


@unittest.skipUnless(shutil.which("kubectl"), "kubectl renders the kustomizations (CI's manifests job has it)")
class TheCronJobSetsNothingElse(unittest.TestCase):
    def test_the_cronjob_job_pod_and_container_set_only_the_fields_the_component_states(self):
        for target in ENVIRONMENTS:
            with self.subTest(target=target):
                spec = one(render(target), "CronJob", "inkcast-env-secrets")["spec"]
                self.assertEqual(set(spec), SPEC, "no suspend: a suspended CronJob never brings a Secret back")
                job = spec["jobTemplate"]["spec"]
                self.assertEqual(set(job), JOB, "one pod per run: no parallelism, completions or another policy")
                pod = job["template"]["spec"]
                self.assertEqual(set(pod), POD, "the pod spec of the init container's run, nothing more")
                (container,) = pod["containers"]
                extra = {"imagePullPolicy"} if target.startswith("overlays/") else set()
                self.assertEqual(set(container), CONTAINER | extra)


if __name__ == "__main__":
    unittest.main()

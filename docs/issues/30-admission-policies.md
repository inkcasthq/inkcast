# Security: enforce admission policies

labels: security,deploy

Move `deploy/policies/kyverno-verify-inkcast-image.yaml` from Audit to Enforce after a soak; add policies for restricted pod security, no `:latest`, required probes/resources.

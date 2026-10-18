# CI/CD follow-ups

labels: ci

The day-1 pipeline exists (docs/cicd.md). Follow-ups: push Playwright captures to a `ci-captures` branch on main (same pattern as the kind + Playwright PoC CI); kubeconform schema validation of rendered manifests; SAST beyond CodeQL (semgrep); branch protection and required checks configured as code; image-scan exceptions file with expiry dates.

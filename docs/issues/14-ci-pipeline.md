# CI: tests, security scans and page captures

labels: ci

GitHub Actions: unit + end-to-end tests, SAST (bandit/semgrep), dependency audit, container image scan.

- Playwright screenshots of the sign-in, consent and connect pages pushed to a `ci-captures` branch (same pattern as the existing kind + Playwright PoC CI)

**Done when:** PRs are blocked on failing tests or high-severity findings.

# CI/CD

## Principles

1. **Build once, deploy by digest.** An image is built a single time, on main or on a release tag. Everything downstream — scan, SBOM, signature, provenance, environments — refers to its `sha256` digest, never to a mutable tag.
2. **Prove origin before deploying.** Every image is:
   - signed keyless (cosign + GitHub OIDC, no stored keys);
   - attested with an SPDX SBOM;
   - recorded with SLSA build provenance.

   Each proof is **read back** in the same run. Promotion re-verifies; a Kyverno admission policy verifies again in the cluster.
3. **GitOps for delivery.** CI never holds cluster credentials. Promotion opens a pull request that pins a digest in `deploy/kustomize/environments/<env>`; Argo CD or Flux applies the merge.
4. **Least privilege per job.** Workflows default to `contents: read`. Only the job that pushes gets `packages: write`; only the job that signs gets `id-token: write`.
5. **Pinned everything.** Actions are pinned by commit SHA with the version in a comment, and Dependabot moves both. Tools are pinned to exact versions (ruff, Playwright, which pins the Chromium build). The API-docs bundles are vendored and hash-locked.
6. **Air-gap friendly.** The build needs only the checkout and PyPI. Nothing is fetched from a CDN at build or run time.

## Pipeline

```
pull request ─► ci.yml ──────────────────────────────────────────────────────────────┐
                 lint (ruff, vendored-asset lock)                                    │ required
                 test (py3.12–3.15; unit + e2e with fakes + real-browser docs check;  │ checks
                       the end-to-end walkthrough on 3.14, see walkthrough.md)          │
                 manifests (render every overlay/environment)                         │
                 image (build + Grype scan → code scanning)                           │
                 kind (deploy real image + manifests under PSS restricted, smoke;     │
                       OpenFGA on CloudNativePG, the migrate Job, write-model)         │
                 codeql (python + js)                                                 ┘

push to main ──► publish.yml: gate (= ci.yml) ► build+push (digest) ► sbom ► attest (sign, SBOM attest,
                 provenance, verify all) ──► promote.yml (workflow_run) ► PR: staging digest bump
                                                                              │ merge
                                                                              ▼
                                                                   Argo CD / Flux syncs staging

tag vX.Y.Z ────► publish.yml (same) ► release: GitHub Release + manifests rendered at the digest
                 promote.yml (manual, environment=production, required reviewers) ► PR ► merge ► sync
```

| Workflow | Trigger | Writes |
|---|---|---|
| `ci.yml` | PRs, weekly schedule, called by publish | code-scanning results only |
| `publish.yml` | push to main, `v*.*.*` tags, manual | registry (build job), signatures and attestations (attest job), releases (release job), the `ci-captures` branch (captures job: dashboard screenshots and the [walkthrough](walkthrough.md)) |
| `promote.yml` | publish succeeded on main, or manual | a branch + PR in this repo |

## Environments and promotion

**Staging**

- Updated by an automatic PR after every green main build.
- The PR's CI waits for a person to approve it. `promote.yml` opens the PR with the workflow's `GITHUB_TOKEN`, and
  GitHub creates the `pull_request` runs that follow "in an approval-required state"
  ([GitHub docs](https://docs.github.com/en/actions/concepts/security/github_token)). Until someone with write access
  approves them, the PR has no check runs and branch protection keeps it blocked. Approve them on the Actions tab
  ("Approve and run"), or with the API:

  ```bash
  gh api "repos/inkcasthq/inkcast/actions/runs?status=action_required&branch=promote/staging-<12 hex>" \
    --jq '.workflow_runs[].id' | while read -r id; do gh api -X POST "repos/inkcasthq/inkcast/actions/runs/$id/approve"; done
  ```

- Merging, once those checks pass, is the deploy.

**Production**

- Updated by a manual `promote.yml` run, using the GitHub Environment `production` with required reviewers.
- The PR changes `deploy/kustomize/environments/production`, which CODEOWNERS protects.

**Rollback** is a revert of the digest PR. The previous digest is still signed and still in the registry.

## Versioning

- **Releases:** SemVer tags `vX.Y.Z`. Image tags are `X.Y.Z` and `sha-<12>`. There is no `latest`.
- **Changelog:** generated from merged PRs in the GitHub Release.
- **Rendered manifests:** both overlays, pinned to the digest, attached to every release. Self-hosting customers can `kubectl apply -f` them.

## Plan: GitHub Team (public or private repository)

On a **public** repository, code scanning, artifact attestations and environment protection rules are free, and the workflows switch them on automatically (`github.event.repository.visibility == 'public'`). The table below applies while the repository is **private**.

| Feature | On Team (private repo) | Used here |
|---|---|---|
| Branch protection / rulesets, CODEOWNERS review | ✅ | Yes: the production gate is a CODEOWNER-approved PR |
| Environments, environment secrets, deployment branches | ✅ | Yes |
| Environment **required reviewers** / wait timers | ❌ (public repos or Enterprise) | No |
| Code scanning (CodeQL, SARIF upload) | Only with **GitHub Code Security** add-on | Switch: `vars.CODE_SECURITY=true` |
| Artifact attestations (GitHub provenance store) | ❌ (Enterprise Cloud) | Switch: `vars.GITHUB_ATTESTATIONS=true` |
| cosign keyless signature + SBOM attestation | ✅ (Sigstore public good) | Always |
| Dependabot, GHCR, Actions | ✅ (minutes included per plan) | Always |

Without the add-ons, the Grype scan still **fails the build** on fixable high or critical findings, and its SARIF is kept as a run artifact. Images are still signed with an SBOM attestation, and promotion and the Kyverno policy verify the signature.

Keyless signing writes the repository and workflow path, not code, to the public Rekor log. If that is unacceptable for a private repo, switch to a KMS-held cosign key.

## Repository settings (do these once)

**Branch protection on `main`:**

- PR required, with 1 approval and CODEOWNER review.
- Required checks:
  - `lint`
  - `test (3.12)`, `test (3.13)`, `test (3.14)`, `test (3.15)`
  - `manifests`
  - `image`
  - `kind`
  - `codeql`, only if Code Security is on
- Linear history; no force pushes.

**Environments:**

- `staging` — no extra rules.
- `production` — deployments limited to `main` and tags. Approval is the CODEOWNER-reviewed promotion PR.

**Variables and secrets:**

- `vars.REGISTRY` (default `ghcr.io`) and `vars.REGISTRY_USER`.
- `vars.CODE_SECURITY` and `vars.GITHUB_ATTESTATIONS` — leave unset on Team.
- `secrets.REGISTRY_TOKEN`, only if not using GHCR with `GITHUB_TOKEN`.

**Security:** Dependabot alerts and security updates on; code scanning on; secret scanning with push protection on.

## Retiring a code-scanning configuration

GitHub keys code-scanning results by **configuration**: the workflow file and job that uploaded them, plus the category, for example `.github/workflows/codeql.yml:analyze`. When a workflow or job is renamed or removed, the old configuration stays on `main` with its last results. Every pull request's `CodeQL` check then warns "1 configuration not found", and GitHub can't tell whether the old configuration's alerts were fixed. Retire it once the new configuration has run on `main`:

1. **In the UI:** Security → Code scanning → Tool status → CodeQL. Under *Configurations*, open the old one, then ⋯ → **Delete configuration**. Its alerts close; alerts that the new configuration also finds stay open there.
2. **Or with the API** (a token with `security_events`, which repository automation doesn't have here):
   ```bash
   gh api "repos/inkcasthq/inkcast/code-scanning/analyses?ref=refs/heads/main&per_page=100" \
     --jq '.[] | select(.analysis_key == ".github/workflows/publish.yml:codeql") | .id' |
   while read -r id; do gh api -X DELETE "repos/inkcasthq/inkcast/code-scanning/analyses/$id?confirm_delete=true"; done
   ```
   An analysis can only be deleted when it's the newest of its configuration, so the loop goes newest first. Repeat it until nothing is listed.
3. Check: the next pull request's `CodeQL` check no longer mentions a missing configuration.

The one to retire today is **`.github/workflows/publish.yml:codeql`**. It's left over from when CodeQL ran inside ci.yml, which publish.yml calls as its gate on `main` (#15, fixed in #20). Its replacement is `.github/workflows/codeql.yml:analyze` (issue #38).

## What runs where locally

| Task | Command |
|---|---|
| Tests | `PYTHONPATH=src:tests python -m unittest discover -s tests` |
| Lint | `ruff check src tests` |
| Code scanning | `scripts/dev/codeql.sh`: CI's CodeQL bundle, config (`.github/codeql/codeql-config.yml`) and suites; prints each result's code flow and exits 1 at the check's failure threshold (security severity high or above, or error) |
| Vendored assets | `scripts/vendor-assets.sh` (offline verify); `--outdated`, `--update`, `--upgrade` |
| Manifests | `kubectl kustomize deploy/kustomize/overlays/openshift` |
| Cluster smoke | `kind create cluster && kind load docker-image inkcast:ci && kubectl apply -k deploy/kustomize/overlays/ci` |
| OpenFGA on kind | `scripts/dev/openfga-e2e.sh` (the CloudNativePG release pinned in `scripts/versions.env`, the `ci-openfga` overlay, `inkcast authz write-model`) |

## Not verified yet

`ci.yml` runs on every PR, and publish → attest → promote on every push to main. Since 2026-10-27, each successful
publish has opened a staging promotion PR (the first was #243). The Grype gate, the GHCR signature and attestation
permissions, and the `kind` job all pass there. Two paths have not run on GitHub yet:

- a manual `promote.yml` run for `production`;
- a `v*.*.*` tag: publish's `release` job.

Follow-ups are in [issues/14](issues/14-ci-pipeline.md), [26](issues/26-helm-chart.md), [28](issues/28-hashed-dependency-lock.md), [29](issues/29-gitops-bootstrap.md) and [30](issues/30-admission-policies.md).

---
<!-- inkcast-nav -->
[Docs](../docs/README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)

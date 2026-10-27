# Bootstrap: from this bundle to a running project

One-time steps for the owner. Do them in order; each takes minutes, except the reviews in step 7, which take weeks.

## 1. Organisations

| Org | Role | Owner |
|---|---|---|
| `inkcasthq` | **Primary**: repo, container images, Actions. GitHub Team plan. | ScalarOps Technologies LLC |
| `getinkcast` | Reserved. Its profile points to `inkcasthq`. | ScalarOps Technologies LLC |

For **both** orgs:

- Settings → Authentication security → **require two-factor authentication**.
- Add a **second owner** (a backup account you control).
- Profile → Name: **Inkcast**. Handles stay lowercase.
- Create a public repo named **`.github`** and add `profile/README.md` from [`org-profiles/`](../org-profiles/).

## 2. Repository

```bash
gh repo create inkcasthq/inkcast --private \
  --description "Write once in markdown, publish everywhere — from your AI assistant"
git clone inkcast.bundle inkcast && cd inkcast
git remote set-url origin git@github.com:inkcasthq/inkcast.git
git push -u origin main
scripts/file-issues.sh inkcasthq/inkcast          # files the roadmap issues in docs/issues
```

## 3. Repository settings

- **Actions → General → Workflow permissions:** "Read repository contents", and Actions may create pull requests (`promote.yml` opens one per image). Each job asks for the extra permissions it needs. `settings.py apply` sets both, but in an organisation the organisation's policy comes first: until an org admin allows Actions to create pull requests, the repository setting is refused with HTTP 409 and `promote.yml` fails. Once, with the owner's `gh` login, before the first `apply`:
  ```bash
  gh auth refresh -h github.com -s admin:org     # reading or changing organisation policy needs this scope
  gh api -X PUT orgs/inkcasthq/actions/permissions/workflow \
    -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true
  scripts/repo/settings.py apply
  ```
  `check` names the organisation policy when it's the cause. It reports the policy as `NOT VERIFIED` when the token can't read it (as with `GITHUB_TOKEN` in the daily workflow).
- **Protect `main` and the security settings, as code:** `scripts/repo/settings.py apply` (admin) sets the ruleset in `.github/rulesets/main.json` and the settings in `.github/repo-settings.json`; the `repo-settings` workflow checks them daily and fails on drift. What they hold:
  - a pull request for every change, squash merge only, linear history, no force pushes or deletion, and no bypass (the owner included);
  - required checks: `lint`, `locks`, `test (3.12)`, `test (3.13)`, `test (3.14)`, `test (3.15)`, `authz`, `manifests`, `image`, `kind`, `dashboards`, `analyze` (CodeQL) and `CodeQL` (its results: a new high alert fails it). A test keeps this list, the ruleset and the jobs in step;
  - approvals: 0 while there is one maintainer (GitHub doesn't let an author approve their own pull request). Set `required_approving_review_count` to 1 and `require_code_owner_review` to true when a second maintainer joins;
  - secret scanning with push protection, Dependabot security updates, vulnerability alerts and private vulnerability reporting on; head branches deleted after merge.
- **Environments:**
  - `staging`;
  - `production`, with deployments limited to `main` and tags.
- **Security:** enable Dependabot alerts and security updates. When **public**, also turn on secret scanning with push protection, private vulnerability reporting and code scanning.
- **Variables:** while the repo is **private** on Team, leave `CODE_SECURITY` and `GITHUB_ATTESTATIONS` unset. They switch on automatically when public; see [cicd.md](cicd.md).
- **Packages:** after the first publish, open `ghcr.io/inkcasthq/inkcast` → Package settings → link it to the repository and set visibility.

## 4. Contributor licence agreements

1. Have counsel approve [`legal/CLA-INDIVIDUAL.md`](../legal/CLA-INDIVIDUAL.md) and [`legal/CLA-ENTITY.md`](../legal/CLA-ENTITY.md), and remove the DRAFT banners.
2. Publish the approved individual CLA as a GitHub gist owned by the org.
3. Install **CLA Assistant** (cla-assistant.io) on `inkcasthq`. Link `inkcasthq/inkcast` to the gist.
4. Make the CLA check a required status check on `main`.

## 5. Before going public

Work through [issue 32](issues/32-legal-review.md) with counsel:

- governing law;
- the licensing contact in [LICENSING.md](../LICENSING.md);
- approval of the additional permissions;
- the "Inkcast" trademark filing;
- an IP ownership memo confirming ScalarOps owns the code to date.

Then switch the repository to public. The CI features switch on by themselves.

## 6. Business set-up

- **Name:** file an assumed name (DBA) "Inkcast" for ScalarOps Technologies LLC.
- **Money:** keep Inkcast's money separate: its own account, or at least its own bookkeeping category.
- **Domains:** register them in the LLC's name, plus a **separate media domain** for the social platforms (see the [social epic](design/social-platforms-epic.md)).
- **Social handles:** reserve the Inkcast handles on X, Instagram and TikTok under the company.

## 7. Platform developer accounts (long lead times; start now)

| Platform | Do now | Why |
|---|---|---|
| Meta (Instagram) | Business Verification for ScalarOps; create the app | App Review needs it; no published timeline |
| TikTok | Register the app; add the Content Posting API; verify the media domain | The audit gates public posting |
| X | Create the console app; prepay credits; set a **spend cap** | Pay-per-post pricing |

## 8. First deployment

Pick an overlay in [`deploy/kustomize`](../deploy/kustomize) and follow the README's *Hosted* section. You need:

- a Keycloak realm (or the GitHub/password sign-in options);
- the `inkcast-secrets` Secret;
- the Route or HTTPRoute host.

Then everyone adds it with `claude mcp add --transport http inkcast https://<host>/mcp`.

---
<!-- inkcast-nav -->
[Docs](../docs/README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)

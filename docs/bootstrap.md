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

- **Actions → General → Workflow permissions:** "Read repository contents". Each job asks for the extra permissions it needs.
- **Branches → protect `main`:**
  - require a pull request, 1 approval and **code-owner review**;
  - require linear history, and allow no force pushes;
  - required checks: `lint`, `test (3.11)`, `test (3.12)`, `test (3.13)`, `manifests`, `image`, `kind`. Add `codeql` once the repo is public or GitHub Code Security is on.
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

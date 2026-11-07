## Release milestone or hotfix
<!-- Name the issue, and say which of the two this is. Set the issue's milestone in the sidebar: an issue's milestone comes
     from its wave in docs/backlog/order.yaml, and `tracker.py --drift` reports a pull request that differs from it. -->
Closes #

- [ ] Release milestone:
- [ ] Hotfix, and why it can't wait for the milestone:

## What and why
<!-- The story (e.g. F2.2.S2) and a sentence on why. -->

## How it was tested
<!-- Test names, captures, CI run. -->

## Before review
- [ ] This description is complete, with notes for the reviewer
- [ ] The pull request has the issue's milestone and a label
- [ ] The issue's acceptance criteria are each met, with the evidence linked here
- [ ] The checks in [CONTRIBUTING.md](../CONTRIBUTING.md) pass locally, and no file outside the issue changed

## Before merge
- [ ] CI is green
- [ ] Reviewed by someone who didn't write it, and each finding is fixed, or accepted with its reason in a comment

## On staging
<!-- After the merge, when the change publishes an image. Otherwise say that it publishes none, and tick these. -->
- [ ] The staging promotion is merged, and the digest it pins holds this change
- [ ] Staging runs that digest, and the checks in [docs/cicd.md](../docs/cicd.md) pass there
- [ ] The change was tried on staging, and it works

## On production
<!-- The digest proven on staging, never a new build. -->
- [ ] The production promotion is merged, with the digest staging ran
- [ ] Production runs that digest, and the checks in [docs/cicd.md](../docs/cicd.md) pass there

## Definition of done (docs/definition-of-done.md)
- [ ] Story and design note in `docs/epics/E<n>-…/stories.md` (diagram if a flow changed; approved mockup before any UI code)
- [ ] Code follows the [contributor map](../docs/contributor-map.md): shared code in `libs/`, no service imports another service
- [ ] Tests: unit + end to end; they pass single-process **and** split (`test_split_mode`); a fake for any new external API; a cross-user negative test if data is touched
- [ ] Security: every route/tool checks a scope; queries are scoped to the caller; no secrets in code, logs, fixtures or tool output
- [ ] Authorization ([docs/authorization.md](../docs/authorization.md)): a new endpoint or MCP tool uses `require()` or declares `permission=…` (public routes say `public=True`); no resource permissions from JWT roles; publish-capable features check `can_publish` (request and send time); model changes come with `authz/model.fga.yaml` tests and `fga model test` passes; plan limits checked via `libs/entitlements`, never tuples
- [ ] Contract first: `libs/contracts` models and OpenAPI updated; errors are problem+json; backward compatible
- [ ] Config from env (`*_FILE` for secrets), checked by `Config.validate`, documented; logs and a metric on new paths
- [ ] `ruff check src tests scripts` and `python scripts/gen-manifests.py --check` are clean
- [ ] Docs updated (README / docs/) and the epic's `implementation.md`

## Contributor
- [ ] I have signed the Inkcast CLA (the CLA Assistant check is green), and my employer/client permits this contribution
- [ ] All code is my own, or its source and (permissive) licence are stated below

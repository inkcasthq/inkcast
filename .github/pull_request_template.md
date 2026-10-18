## What and why
<!-- The story (e.g. F2.2.S2) and a sentence on why. -->

## How it was tested
<!-- Test names, captures, CI run. -->

## Definition of done (docs/definition-of-done.md)
- [ ] Story and design note in `docs/epics/E<n>-…/stories.md` (diagram if a flow changed; approved mockup before any UI code)
- [ ] Code follows the [contributor map](../docs/contributor-map.md): shared code in `libs/`, no service imports another service
- [ ] Tests: unit + end to end; they pass single-process **and** split (`test_split_mode`); a fake for any new external API; a cross-user negative test if data is touched
- [ ] Security: every route/tool checks a scope; queries are scoped to the caller; no secrets in code, logs, fixtures or tool output
- [ ] Contract first: `libs/contracts` models and OpenAPI updated; errors are problem+json; backward compatible
- [ ] Config from env (`*_FILE` for secrets), checked by `Config.validate`, documented; logs and a metric on new paths
- [ ] `ruff check src tests scripts` and `python scripts/gen-manifests.py --check` are clean
- [ ] Docs updated (README / docs/) and the epic's `implementation.md`

## Contributor
- [ ] I have signed the Inkcast CLA (the CLA Assistant check is green), and my employer/client permits this contribution
- [ ] All code is my own, or its source and (permissive) licence are stated below

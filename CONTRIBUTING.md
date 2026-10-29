# Contributing to Inkcast

Thanks for helping. Issues, platform plug-ins, docs and fixes are all welcome.

## Before your first pull request: sign the CLA

Every contributor signs the [Inkcast Contributor License Agreement](legal/CLA-INDIVIDUAL.md) once. The CLA Assistant bot asks you on your first pull request; it takes a minute.

**Why.** Inkcast is source-available under PolyForm Shield and is also offered commercially by ScalarOps Technologies LLC. The CLA lets ScalarOps ship your contribution under both, including relicensing and selling it, so one codebase serves everyone. You keep your copyright, and your name stays in the history.

**If you're employed or contracting**, your employer or client may own what you write, even on your own time. Check your agreement first. If it does, ask them to sign the [Entity CLA](legal/CLA-ENTITY.md) and list you, or get their written permission.

**Don't submit code you didn't write** unless you say where it came from and under what licence. It must be compatible: MIT, BSD, Apache-2.0 or similar. Nothing GPL/AGPL, and no code copied from other products.

## Workflow

0. Read the [contributor map](docs/contributor-map.md) (where your change goes, the four rules the tests enforce, step-by-step recipes) and the [definition of done](docs/definition-of-done.md).
1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Fork, branch from `main`, and keep each pull request focused.
3. Run the checks locally, from the checkout's virtual environment `.venv`:
   ```bash
   python3 -m venv .venv               # once; Python ≥ 3.12 (CI and the image run 3.14)
   .venv/bin/pip install -e . 'ruff==0.12.0' 'playwright==1.56.0'   # what CI installs
   .venv/bin/python -m playwright install chromium
   PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests
   .venv/bin/ruff check src tests scripts
   .venv/bin/python scripts/gen-manifests.py --check   # split-service manifests match the code
   .venv/bin/python scripts/gen-alerts.py --check      # PrometheusRule matches the alert catalogue (libs/alerting.py)
   .venv/bin/python scripts/gen-dashboards.py --check  # the Grafana dashboard matches libs/dashboards.py
   .venv/bin/python scripts/gen-realm-imports.py --check  # the lab's KeycloakRealmImports match the realm file and the environments
   .venv/bin/python scripts/backlog/tracker.py --check    # every open story has a place in the order; docs/backlog/TRACKER.md is current
   scripts/vendor-assets.sh            # vendored API-docs bundles still match their lock
   scripts/dev/codeql.sh               # CodeQL as CI runs it, each result with its source-to-sink path
   ```
   **Every Python command runs from `.venv`**, by its path as above or after `source .venv/bin/activate`. A
   machine's own `python3` usually lacks Inkcast's dependencies, and then every test fails at import
   (`ModuleNotFoundError: No module named 'cryptography'`, measured), which says nothing about the code. A git
   worktree without a `.venv` of its own uses the main checkout's. `scripts/dev/walkthrough/run_walk.sh` picks
   `.venv` by itself.
   The CodeQL run downloads the bundle CI uses once (about 700 MB, SHA-256 checked) and takes a few minutes. Run it
   before pushing anything that touches secrets, tokens, logging, files or HTTP: a result there fails the pull
   request's CodeQL check.
4. **Add tests.** New platform plug-ins come with a fake of the platform's API (see `tests/fakes.py`) and a guide-compliant `prepare()`. Read [docs/writing-a-platform-plugin.md](docs/writing-a-platform-plugin.md).
5. **Write clear commits** that say why, not only what.

## Ground rules

- **Never put secrets in code, tests, fixtures, logs or tool output shown to the AI.** Platform credentials only ever pass through the one-time connect page.
- **Keep the security posture:** consent on every authorization, hashed tokens, encrypted credentials, strict CSP on pages, no CDN assets.
- **Keep dependencies few and permissively licensed.** Explain any new one in the pull request.
- **Follow the house conventions** in the [contributor map](docs/contributor-map.md#house-conventions): catalogued log events, typed settings, rate limits on unauthenticated endpoints, and no blind retries.

## Reporting security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md).

## Conduct

Be respectful and constructive. We follow the [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/); report concerns to the licensing contact in [LICENSING.md](LICENSING.md).

---
<!-- inkcast-nav -->
[Docs](docs/README.md) · [Licensing](LICENSING.md) · [Legal](legal/README.md) · [Security](SECURITY.md) · [Inkcast README](README.md)

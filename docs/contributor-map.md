# Contributor map: where does my change go?

Inkcast is one codebase that runs as several services. The structure keeps changes small and safe: most changes touch one folder. Tests catch the mistakes that are easy to make.

```
src/inkcast/
  launcher.py          runs services by name (INKCAST_APP_NAME); you rarely touch this
  cli.py               `inkcast sync`, the client CI uses
  libs/                SHARED LIBRARIES: used by every service, never imports a service
    config.py            every setting, read from the environment; validate(needs) per service
    secrets/             the bottom layer: SecretValue, fetchers (env/_FILE, dir, AWS agent…), SecretsProvider,
                         value-based Redactor, naming convention, posture checks (docs/reference/secrets.md)
    observability.py     JSON/text logs (every line scrubbed), request ids, the EVENTS catalogue, metrics
    auth/                Principal, scopes, TokenVerifier (OAuth tokens + personal access tokens)
    db/                  the store: one repository per domain, each owned by one service
    content/             parsing markdown stories (front matter, overrides, images, schedule)
    platforms/           the Platform plug-in interface + Medium, DEV
    contracts/           API models, the OpenAPI document, the public route table
    http/                app composition (/ or /<full-name>), request-id/body-limit middleware, rate limits,
                         probes, REST auth + errors, pages, API docs
    pipeline.py          the publish pipeline: prepare → send → record (used by API and scheduler)
    runtime.py           the per-process Context every service is built from
    service.py           ServiceSpec: what a service is
  services/            SERVICES: each exposes SERVICE = ServiceSpec(...); never imports another service
    identity/            OAuth server, sign-in, consent, personal access tokens
    publishing/          REST /api/v1, MCP /mcp, previews, Git publishing (sync.py)
    connectors/          one-time "connect a platform" pages, credential vault
    scheduler/           sends scheduled posts, heartbeats, housekeeping
    gateway/             built-in edge: routes public paths to services
tests/                 unit + end-to-end tests; fakes.py has fake Keycloak, Medium and DEV
deploy/kustomize/      Kubernetes/OpenShift manifests; components/split-services is GENERATED
scripts/               gen-manifests.py, vendor-assets.sh, ci-run.sh, backlog filing
docs/                  you are here; epics/ hold design, stories, implementation and review per epic
```

## The four rules (tests enforce them)

1. **`libs/` never imports `services/`.** If two services need something, it goes in `libs/`.
2. **A service never imports another service.** Services share contracts (`libs/contracts`), the store (`libs/db`) and HTTP. That is what lets each one run on its own (`tests/test_split_mode.py` proves it).
3. **Every route and tool is authorized.** Today: `Authed(scope, handler)` for REST and `principal(scope)` in MCP tools. With E3: the one `authz.require(principal, relation, ref, scope=…)` path, which checks the token scope **and** OpenFGA. Routes declare `permission=…` or `public=True`, and a test fails any route that declares neither. There is no unauthenticated data route. See [authorization.md](authorization.md).
4. **Secrets never leave as plain text.** Store hashes (`token_hash`) or sealed values (`Sealer`), and hold them in memory as `SecretValue`. Call `.reveal()` only where the credential is used; a test keeps the list of those places. Every loaded secret is registered with the Redactor, so it's masked in every log line and traceback. Never return a credential from an API or a tool; show new tokens only on a browser page.

## House conventions

- **Log events, not prose.** Use `log.info("…", extra={"event": "publish.recovered", …})`, with the name from `observability.EVENTS`. A test fails on an uncatalogued or unused event, so alerts never match on reworded text.
- **Typed settings.** New settings go through `_Env` in `libs/config.py`, which is typed and raises `ConfigError` naming the variable. A new credential also goes in `SECRET_SETTINGS` with the `needs` it belongs to.
- **Unauthenticated endpoints are rate-limited**: wrap them with `limited()` from `libs/http/ratelimit.py`.
- **Before any retry**, ask: "could the first attempt have succeeded?" If yes, confirm with the platform (`find_existing`) or mark the post `unconfirmed`. Never re-send blindly.
- **A time written is a time kept.** A Git file with a `publish_at` is always a scheduled post at that time, due at once if it has passed, and nothing is sent before its written time; only a file with no `publish_at` is a "now" post. A person publishing by hand is refused a past or near time instead ([ADR-0015](adr/0015-publish-at-is-always-scheduled.md)).
- **Check your config before deploying:** `inkcast checks` prints the security posture table.

## Recipes

### Add a REST endpoint
1. Add request/response models to `libs/contracts/models.py`.
2. Describe the operation in `libs/contracts/openapi.py`, including its scope and owning service.
3. Write the handler in the owning service (`services/<svc>/rest.py` or `__init__.py`) and wrap it: `authed("posts:read", handler)`.
4. If the path is new at the top level, add it to `libs/contracts/routes.py`, then run `python scripts/gen-manifests.py`.
5. Test it end to end in `tests/` (it runs in both modes automatically if you add it to an existing harness class).

### Add a platform (e.g. Hashnode)
Follow [writing-a-platform-plugin.md](writing-a-platform-plugin.md). Subclass `Platform`, keep `prepare()` pure, and use `self.request(...)` so ambiguous failures become `unconfirmed` instead of duplicates. Declare `capabilities = {"update", "find"}` only if the API really supports them. Add a fake to `tests/fakes.py`.

### Add a service
1. Create `services/<name>/__init__.py` with `SERVICE = ServiceSpec(name="CamelCase", summary=..., routes=... or workers=..., needs=(...))`.
2. Register it in `launcher.BUILTIN` (built-in) or under the `inkcast.services` entry point (plug-in).
3. If it serves public paths, add them to `libs/contracts/routes.py` and run `python scripts/gen-manifests.py`.
4. `inkcast services` now lists it; `INKCAST_APP_NAME=<Name>` runs it.

### Change who may do what

1. Edit `authz/model.fga`, and add or adjust tests in `authz/model.fga.yaml`, including a negative case.
2. Run `fga model test --tests authz/model.fga.yaml`, then regenerate `authz/model.json` (`fga model transform --file authz/model.fga --output-format json | python3 -m json.tool > authz/model.json`).
3. Never add a role column, a role check on JWT claims, or business state (approval, plan) as a tuple.

### Add a setting
1. Add a field with a safe default to `Config` in `libs/config.py`, and read it in `load_config()`. Secrets use `_env("NAME")`, which also reads `NAME_FILE`.
2. If a wrong value is dangerous, check it in `Config.validate()` under the right `needs` key, so startup refuses with exit 78.
3. Document it in the README's configuration table.

## Before you open a pull request

```bash
PYTHONPATH=src:tests python -m unittest discover -s tests   # everything, both modes
ruff check src tests scripts
python scripts/gen-manifests.py --check                     # if you touched services or routes
```

Then go through the [definition of done](definition-of-done.md).

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)

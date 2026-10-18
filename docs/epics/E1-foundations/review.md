# E1 + E2: independent review

**Reviewer:** Fable 5.1, acting as an independent senior architect and security reviewer. It didn't write the code under review. It reviewed the code end to end against the plan, the [definition of done](../../definition-of-done.md) and the owner's reference repository (`aws-secrets-manager-node-poc`). It covered both epics together, because they shipped in one branch.

**Process:** every finding came with its fix as a patch file. The lead engineer applied them in order (`git am`, commits `30ff9c5`…`28db5a5`, authored as the owner), re-ran the full suite, and checked each patch's risk notes.

## Resolution

| Severity | Found | Fixed | Accepted (reason in §5) |
|---|---|---|---|
| P0 | 1 | 1 | 0 |
| P1 | 6 | 6 | 0 |
| P2 | 11 | 9 | 2 (F-17 SQLite on the event loop → E3; F-18 consent phishing is inherent to OAuth, mitigated by the consent page) |

**Tests:** 80 → 118, all passing; ruff clean; manifests current. **No P0 or P1 is open, so E1 and E2 meet epic DoD #4.**

E2-specific findings, for that epic's record:
- F-06: an interrupted send must be confirmed, never re-sent.
- F-10: one token lookup per call.
- F-11: no PAT over plain http.
- F-12: rate limits on token creation and the other unauthenticated endpoints.

The rest of this page is the reviewer's report, unedited apart from headings.

### 1. Executive summary

Inkcast's structure is sound: one codebase, services that only share `libs/`, an enforced import rule,
hashed bearer secrets, sealed platform credentials, a ledger that claims before it sends, probes on a
private port, Kustomize that already mounts secrets as files with `defaultMode: 0440`. The security model
(OAuth 2.1 with consent, scoped PATs that can't mint PATs, tokens shown only in the browser) is the right
one and is implemented consistently across REST and MCP.

What was missing, and what the patches add:

1. **The reference secrets design was only a sketch.** `libs/secrets.py` had a `SecretValue` and a
   provider nobody called; `Config` held four credentials as plain strings (so `repr(config)` printed them)
   and read them once at import time. Patches 1–6 implement the reference design in Python, from the
   get-go: a `libs/secrets/` package with fetchers behind one interface (env/`NAME_FILE`, a mounted
   directory, the AWS agent on loopback, boto3 and Vault stubs), a naming convention with validation,
   "config files hold names only", a `SecretsProvider` with TTL + jitter, single-flight, bounded
   stale-on-error, `refresh(name, reason)`, change events, HMAC fingerprints and a value-free `status()`, a
   value-registering `Redactor` behind every log line, startup posture checks behind the exit-78 gate, and
   `GET /secrets` (metadata only) on the metrics port. The four credentials now flow through the provider
   as `SecretValue` with `.reveal()` at the point of use, and an architecture test keeps the list of files
   allowed to call `reveal()`.
2. **One P0 found by CI, fixed inside the wiring patch:** a Gateway pod receives the shared ConfigMap's
   `INKCAST_PASSWORD_FILE` but mounts no secrets, and `load_config()` died on the unreadable file before
   `validate(needs)` could decide whether it mattered. Secret read errors are now deferred and reported
   only for the services that need that credential; the Gateway starts, IdentityService exits 78.
3. **Four P1 defects.** (a) Text-format logs (the laptop default) never redacted tracebacks, so an IdP
   error carrying `?code=…` or a client secret went out verbatim. (b) The built-in Gateway copied the
   client's `X-Forwarded-For` chain and `X-Forwarded-Proto` to the services, so a caller could claim
   another address or https. (c) No body-size limit anywhere but the Gateway, which read the whole body
   before measuring it. (d) The scheduler's crash recovery moved a post that was mid-send back to the
   queue and sent it again: the exact duplicate the `unconfirmed` state exists to prevent. (e) Reading
   the configuration ran outside the exit-78 gate, so a bad `NAME_FILE` or a non-numeric port was a
   traceback with exit 1.
4. **Observability gaps closed:** request ids propagated from the Gateway through every service's log
   line, a structured access log without query strings, generic `extra=` fields in JSON, and an event
   catalogue with a test that fails on an uncatalogued or unused event name (the reference's contract).
5. **Rate limits** on the four endpoints anyone can call without a token.

Counts: **P0: 1, P1: 6, P2: 11** (18 findings, 16 fixed by patch, 2 accepted with reasons in §5).
Tests: 80 → 118, all green on 3.13 in the sandbox; `ruff check src tests scripts` clean;
`scripts/gen-manifests.py --check` current.

### 2. Reference gap table

Reference feature → Inkcast status at `a4705ee` → what the patches do.

| Reference feature (file) | Inkcast at a4705ee | Now | Patch |
| --- | --- | --- | --- |
| `SecretValue` with `reveal()`, safe `toString`/`toJSON`/`inspect` (`secrets/SecretValue.js`) | partially: `repr`/`str` safe; `json.dumps` raised a generic error; `==` with str impossible; `Config` printed secrets | adopted: f-string/`%r`/`format` safe, `json`/`pickle` refuse, deepcopy-safe, constant-time `==` with str, `Config.repr` safe | 1, 4 |
| Value-based `Redactor`: register loaded values and their encodings, scrub every outgoing string; `registerSecretObject`; `scrub()` of structures and errors (`secrets/redactor.js`) | missing (pattern-only filter on the message) | adopted: `libs/secrets/redactor.py`, process-wide `REDACTOR`, JSON/URL encodings, LRU of 200, `scrub_value()` for structures and exceptions; wired into both formatters on the whole line | 1, 7 |
| `isSensitiveKey` with benign-suffix rules (`redactor.js`, `checks.js`) | missing | adopted, extended to `_FILE/_ID/_DIR/_PATTERN…` pointers | 1 |
| `mask.js`: HMAC fingerprint under a per-process key, fully/partly mask, `describeKeys` | missing | adopted (`libs/secrets/mask.py`); used by the provider's DEBUG "secret consumed" line and `status()` | 1, 3 |
| Fetcher interface, SDK and agent fetchers, loopback-only SSRF token read per call, `refreshNow` (`secrets/fetchers.js`) | missing (env + `NAME_FILE` inline in the provider) | adopted: `Fetcher` ABC; `EnvFetcher`, `DirectoryFetcher` (new: Kubernetes Secret volume, rotation picked up), `AgentFetcher` (real, httpx2, loopback-only), `AwsSdkFetcher` (boto3 optional, clear NotInstalledError), `VaultFetcher` (stub), `ChainFetcher`, `fetcher_from_environ` | 2 |
| `SecretsProvider`: TTL + jitter, single-flight, stale-on-error bounded by max stale, `refresh(id, reason)`, `loaded` event, `startupDone`, `status()` without values, `describe()` (`secrets/provider.js`) | partially: TTL/jitter/single-flight/stale existed but unbounded staleness, no refresh/events/status, not wired | adopted in full; `aget`/`arefresh` for async callers; a forced refresh never serves stale; 30 s retry window | 3 |
| Naming convention with presets, `{env}` mandatory, segment validation, ARN-suffix warning, explicit ids, allowed envs (`secrets/naming.js`, `docs/reference/secret-naming.md`) | missing | adopted and adapted: `INKCAST_SECRETS_NAME_PATTERN`, `INKCAST_SECRET_ID_<NAME>`, `INKCAST_SECRETS_ENV/_APP/_ALLOWED_ENVS`, `secret_slug()` | 2 |
| Config file loader that refuses credentials (`loadConfigFile`, `assertNoCredentials`) | missing | adopted: `INKCAST_CONFIG_FILE` (.env/.yaml/.json via PyYAML), names only, real env wins | 2, 4 |
| `config.js`: non-secret config only, `FORBIDDEN_ENV`, `assertSafeEnvironment` with production rules, exit 78 | partially: `validate(needs)` and exit 78 existed; no environment notion; reading config was outside the gate | adopted: `INKCAST_ENV`, production rules (reveal flag), reading inside the gate; credentials in env become a posture warning rather than a refusal (env is a documented path for Inkcast; see §5) | 4, 5, 6 |
| `security/checks.js` posture checks (static keys, creds file, env secrets, IMDSv2, identity, non-root, inspector, core dumps, transport, TLS, reveal flag, redaction, log delivery) | missing | adopted the applicable ones: https URL, key strength, reveal/debug flags, JSON logs, secret-file modes, env scrubbed, redaction active, non-root, core dumps, forwarded IPs, login method, database file mode, secrets source. AWS-only checks (IMDS, STS, static keys) are not applicable | 5 |
| `log.js`: redaction twice per record (structure + final line), sinks receive only the redacted line, `revealUnsafe` as the one unredacted path, `child()` bindings | partially: JSON formatter redacted message and traceback; text formatter redacted the message only | adopted: both formatters scrub the final rendered line; `revealed=True` records are the one exception, refused in production; `extra=` instead of `child()` (Python idiom) | 7 |
| `http.js`: per-request `X-Request-Id`, security headers, 16 KiB JSON body cap, last-mile scrub on responses, `expose` errors | partially: problem+json and CSP on pages; no request id, no body cap, no HSTS/nosniff | adopted: `RequestContext` middleware (request id accepted/generated/echoed, access log, body limit, nosniff/no-referrer/HSTS); Gateway forwards the id | 8 |
| `events.js` in-memory timeline | not applicable (no dashboard yet); the `on_change` callback and `secrets.changed` event are the hook | — | 3 |
| Alert catalogue (`alerts/catalogue.js`), test that every logged event is catalogued and vice versa | missing | adopted the contract: `observability.EVENTS` + test; the Prometheus-rule generator stays for E7 | 7 |
| `alerts/notifier.js`, `recipients.js` (SNS) | not applicable (E7; no SNS in the BEN/Kubernetes target) | — | — |
| `logship/*` (OTLP, file sink) | not applicable (stdout → Fluent Bit, by design) | — | — |
| Deploy: non-root, read-only root, drop caps, seccomp, `automountServiceAccountToken: false`, `enableServiceLinks: false`, liveness vs readiness, secrets never in env | adopted already | unchanged | — |
| CI: supply-chain gate, kind, captures, docs check | adopted already (grype, kind, codeql, captures) | `INKCAST_ENV=ci` in the CI overlays | 6 |
| `HANDOVER.md` / `CLAUDE.md` working rules, backlog-as-code, generated files checked in CI | adopted (owner is writing docs in parallel) | `docs/reference/secrets.md` added | 12 |

### 3. Findings

Severity: P0 must fix before merge; P1 fix in this PR; P2 fix soon or accept. File:line refers to the base
(`25ab174`, identical to `a4705ee` for these files unless noted).

| ID | Sev | File:line | Issue | Failure scenario | Fix |
| --- | --- | --- | --- | --- | --- |
| F-01 | P0 | `libs/config.py:153,161` (`_env` raising at load) | Secret files are read eagerly at `load_config()`; an unreadable `NAME_FILE` raises before `validate(needs)` | Split mode in kind: the Gateway gets `INKCAST_PASSWORD_FILE` from the shared ConfigMap, mounts no secrets, crash-loops | Patch 4: deferred `secret_errors`, reported by `validate(needs)` per credential; test `test_unreadable_secret_files_only_stop_the_services_that_need_them` |
| F-02 | P1 | `libs/config.py:45,57,64,66` | `Config` holds four credentials as `str`; dataclass `repr` prints them; nothing registers them for redaction | `log.debug("config %r", config)` or an exception that formats a `Config` writes every credential to the log | Patch 4 (`SecretValue` fields, `repr=False` provider, registration in `__post_init__`), patch 1 |
| F-03 | P1 | `libs/observability.py:42,66` | Text format never redacts tracebacks: the filter cleans `getMessage()`, `Formatter.format` appends `exc_text` afterwards | On a laptop (`INKCAST_LOG_FORMAT=text`), `httpx2.HTTPStatusError` for the IdP token endpoint carries the request URL with `code=…`; a library message with a client secret goes out verbatim | Patch 7: `TextFormatter`/`JsonFormatter` scrub the whole rendered line with both layers; test `test_registered_values_are_masked_in_fields_messages_and_tracebacks_in_both_formats` |
| F-04 | P1 | `services/gateway/__init__.py:56-59` | The Gateway strips inbound `X-Forwarded-*` from the forwarded list but then rebuilds `X-Forwarded-For` from the client's prior chain and `X-Forwarded-Proto` from the client's header | A caller sends `X-Forwarded-For: 1.2.3.4` / `X-Forwarded-Proto: https`; services and logs see a false address and scheme (log poisoning, any future IP allow-list bypass) | Patch 8: set only from `request.client`/`request.url.scheme` (what uvicorn established from trusted proxies), drop `Forwarded`, `X-Real-IP`, client `X-Request-Id`; test `GatewayProxy` |
| F-05 | P1 | `libs/http/rest.py:47`, `services/gateway/__init__.py:51` | No request body limit in services; the Gateway reads the entire body into memory and only then checks `MAX_BODY` | A 1 GB POST to `/api/v1/posts` (or through the Gateway) is buffered in full; several in parallel OOM the pod (256 Mi limit) | Patch 8: `RequestContext` refuses `Content-Length` over `INKCAST_MAX_BODY_BYTES` before any handler, cuts chunked bodies at the limit; Gateway streams with a cap |
| F-06 | P1 | `libs/pipeline.py:185`, `libs/db/posts.py:68` | `recover_stuck` moves posts left `publishing` for 10 min back to `scheduled` | A SIGKILL/OOM after DEV accepted the POST but before the row was updated → the next tick posts the story again (the duplicate the research warned about) | Patch 9: `claim_stuck` → `recovering`; `Publisher.recover` confirms with `find_existing`, re-queues only a *missing* post, otherwise `unconfirmed`; event `publish.recovered`; test `PipelineRecoveryTests` |
| F-07 | P1 | `launcher.py:134,142` | `load_config()` runs before `setup_logging` and outside the `try` that exits 78 | `INKCAST_METRICS_PORT=nine` or a dangling `NAME_FILE` is a traceback with exit 1, unformatted; systemd/Kubernetes restart-loop instead of fail-fast | Patch 6: logging first, `load_config` inside `refuse()`; patch 4: numeric conversions raise `ConfigError`; test `test_bad_config_exits_78_before_serving` (extended) |
| F-08 | P1 | `libs/observability.py` (no request id), `services/gateway/__init__.py` | No request/correlation id anywhere; `uvicorn.access` silenced, so no access log at all; JSON formatter emits only four hard-coded extras | A failed publish behind the Gateway cannot be tied to the client request across two services' logs | Patch 8 (`X-Request-Id` accepted/generated/echoed/forwarded, `http.request` event with method/path/status/ms, no query string), patch 7 (contextvar in every line, generic extras) |
| F-09 | P2 | `services/identity/__init__.py:109,136` | CSRF token compared with `!=` | Not exploitable over a network with a 24-byte random token, but it is the pattern a junior copies into a password check | Patch 10: `hmac.compare_digest` |
| F-10 | P2 | `libs/auth/__init__.py:59,78` | `access_token()` looks a PAT up twice (two queries, two `last_used_at` writes per MCP call) | Latency and write amplification on the single SQLite writer under MCP load | Patch 10: `lookup()` returns `(principal, row)` once |
| F-11 | P2 | `cli.py:134` | `inkcast sync` sends the PAT to any `INKCAST_URL`, including `http://` | A mistyped URL hands the token to the network in the clear | Patch 10: refuse plain http to a non-localhost host (exit 2) |
| F-12 | P2 | `services/identity/__init__.py:120-130` (`/tokens/new`), MCP SDK `/register`, `services/connectors/__init__.py:28`, password `POST /auth/callback` | Unauthenticated endpoints that write rows or verify credentials have no bound | Row flooding of `pending_auth`/`oauth_clients`; unbounded password guessing (the 1 s sleep is per request, not per client); platform-verification spam | Patch 11: per-client token bucket (`INKCAST_RATE_LIMIT_PER_MINUTE`), 429 problem+json with `Retry-After`; test `RateLimits` |
| F-13 | P2 | `libs/config.py:153-195` | Numeric settings parsed with bare `int()`/`float()` | Operator typo → traceback (see F-07) | Patch 4: `_Env.int/float` raise `ConfigError` naming the variable |
| F-14 | P2 | `libs/observability.py` (no catalogue) | No structured event names; alert rules would match on free text | A reworded log message silently breaks an alert | Patch 7: `EVENTS` catalogue; test enforces both directions |
| F-15 | P2 | `libs/secrets.py` (whole file) | No posture checks, no `INKCAST_ENV`, reveal/debug flags possible anywhere | A production pod with a 32×`k` key or `INKCAST_LOG_LEVEL=DEBUG` starts without a word | Patch 5 + 6: checks behind the gate, `inkcast checks`, image defaults to `INKCAST_ENV=production` |
| F-16 | P2 | `libs/secrets.py` (no status) | No way to see which secret a process holds, from where, and whether rotation was picked up | On-call cannot tell a stale credential from a wrong one | Patch 6: `GET /secrets` on the metrics port (names, sources, fingerprints, ages, errors, posture) |
| F-17 | P2 | `libs/db/database.py` | All SQLite calls run synchronously on the event loop | Latency under load; not a correctness bug (WAL + `BEGIN IMMEDIATE` + RLock are right) | Accepted for E2; E3 (Postgres, a driver with a pool / `asyncio.to_thread`) is the fix. Documented in §5 |
| F-18 | P2 | `services/identity/__init__.py` (`/auth/login?pending=…`) | A pending id is only in the URL; no browser-session binding | OAuth consent phishing: an attacker starts a flow and sends the victim the link; the victim must then approve a consent page naming the attacker's redirect host | Accepted: inherent to OAuth; the consent page names the client and redirect host and says "only approve if you just started this". A cookie binding would not change the human decision. Documented in §5 |

Checked and found correct (no finding): tenant scoping of every repository query (`user_id` on posts,
ledger, credentials, tokens); PAT scope intersection with `SCOPES`; `oauth_only` on token management; REST
is Bearer-only (no cookie, so no CSRF); no user-controlled URL is ever fetched (SSRF); redirect targets
come from the MCP SDK's validated `redirect_uri` or fixed paths; tokens stored as SHA-256 and looked up by
hash (no timing side channel); `email_verified` required for domain allow-lists; refresh-token rotation
by family; `claim_new` before any create in `sync.py` and `claim_due_posts` atomic for replicas; the
`Idempotency-Key` fingerprint check; `connect` page CSP; vendored docs assets hash-locked; Dockerfile
non-root, `HOME=/tmp`, group-0 writable `/data`; Kustomize `readOnlyRootFilesystem`, dropped caps,
seccomp, no SA token, secrets as files with `0440`, metrics port only open to the monitoring namespace;
the GitHub Action runs the sync with the token only in env and dry-runs on pull requests.

### 4. Junior contributor guardrails

What now stops a junior from breaking something silently, and where to look:

| Guardrail | Where | What it catches |
| --- | --- | --- |
| `reveal()` allow-list | `tests/test_architecture.py::test_reveal_is_only_called_where_a_credential_is_used` | a new place where a secret leaves its wrapper; adding one is a reviewed line in the test |
| `libs/secrets` is the bottom layer | `test_secrets_is_the_bottom_layer` | an import cycle (secrets → config → secrets) that would break every process at start |
| Event catalogue | `tests/test_libs.py::test_every_event_name_is_catalogued_and_used`, `observability.EVENTS` | an `extra={"event": …}` that no alert rule knows, or a catalogued alert nobody logs |
| Licence header | `test_every_source_file_has_the_licence_header` | a new file without the SPDX lines |
| `Config.__post_init__` | `libs/config.py` | a credential added as a plain `str` field still gets wrapped and registered; `validate()` is the only place that measures it |
| `SECRET_SETTINGS` map | `libs/config.py` | a new credential must say which `needs` it belongs to, or no service will ever report it missing |
| `Fetcher` ABC + `BACKENDS` | `libs/secrets/fetchers.py` | a new source has three methods to implement and one dict entry; the test file shows a fake transport |
| `CHECKS` tuple | `libs/secrets/checks.py` | a new posture check is one function; a crashing check becomes `skip`, never a crashed process |
| `RequestContext` | `libs/http/middleware.py`, `compose()` | every service gets request ids, body limits and headers without remembering to add them |
| `limited()` | `libs/http/ratelimit.py` | one wrapper for an endpoint function or an SDK ASGI route; reads are never limited by accident |
| `Publisher.recover()` | `libs/pipeline.py` | the one place that decides what an interrupted post becomes; its docstring says why it never re-queues blindly |
| `inkcast checks` | `launcher.py` | a junior can see the posture table for their config before deploying |
| `docs/reference/secrets.md` | docs | the sources, the order, the naming rules, what a secret looks like in logs |

Conventions worth writing into `CONTRIBUTING.md` (I did not edit it, the owner is writing docs):
log with `extra={"event": …}` from the catalogue, never a credential in a message; new config settings
go through `_Env` (typed, `ConfigError` on bad input); new unauthenticated endpoints get `limited()`;
any new "retry" path must answer "could the first attempt have succeeded?" before it re-sends.

### 5. Deliberately not done, and why

- **Credentials in environment variables are a warning, not a refusal** (the reference refuses with
  `FORBIDDEN_ENV`). Inkcast documents `INKCAST_SECRET_KEY=…` as a supported path for laptops and the CI
  overlays use `NAME_FILE`; refusing would break the README flow. The posture check names the variable and
  the fix; production deployments already mount files.
- **World-readable secret files warn rather than fail.** Kubernetes' default `defaultMode` for a Secret
  volume is `0644`; ESO-created Secrets mounted by someone else's manifest would otherwise refuse to start.
  Inkcast's own manifests use `0440`, and the check says exactly which file to fix.
- **boto3 / Vault clients.** No new dependencies were allowed and the sandbox cannot reach PyPI. The agent
  fetcher is real (plain HTTP on loopback, like the reference); the SDK fetcher works if boto3 is installed
  and otherwise exits 78 with the alternative; Vault is a reserved seam. The `dir` source covers the Vault
  Agent injector and ESO today.
- **SQLite calls on the event loop (F-17).** Moving every repository call to a thread is E3's job (Postgres
  driver, pool); doing it now in SQLite with one connection and an RLock would serialise anyway.
- **Consent phishing (F-18).** Inherent to OAuth authorisation-code flows; the mitigation is the consent
  page's wording, which already names the client and the redirect host.
- **Rate limiting at the edge.** The in-process limiter is one node's share; Gateway API / the ingress should
  enforce the real limit. The patch says so in its docstring.
- **Purging `oauth_clients`.** Dynamic registration rows are never deleted. With the limiter this is a slow
  leak, not an attack surface; a housekeeping delete of clients with no tokens for 30 days belongs with E3's
  migrations.
- **Alert rules generator (`sync-alert-rules`) and `deploy/alerts/catalogue.yaml`.** The event catalogue is
  the source; generating PrometheusRules is E7 (the plan says so). I kept `EVENTS` in Python so the test can
  enforce it today.
- **Non-latin-1 upstream headers at the Gateway** would raise on `encode("latin-1")`. RFC 9110 forbids them
  and httpx would not have parsed them; left as is.
- **Docs other than `docs/reference/secrets.md`**, `docs/epics`, `docs/adr`, `docs/backlog`, `README`,
  `src/inkcast/static`: untouched, per the brief.

### 6. Final test and lint output

```
$ cd <clone> && PYTHONPATH=src:tests python -m unittest discover -s tests
----------------------------------------------------------------------
Ran 118 tests in 24.684s

OK
$ ruff check src tests scripts
All checks passed!
$ python scripts/gen-manifests.py --check
manifests current (exit 0)
```

Per-commit check (each patch applied in order on `25ab174`): `ruff check src tests scripts` and
`tests.test_secrets tests.test_libs tests.test_architecture tests.test_units` green at every step; the full
suite green at the tip. The series is based on `origin/feat/e1-services-e2-git-publishing` at `25ab174`
(which includes the lead engineer's `cli.py` change) and was also applied with `git am` on a fresh clone at
the later tip `a4bfc64`: 118 tests OK, ruff clean.

---
<!-- inkcast-nav -->
[Docs](../../README.md) · [Licensing](../../../LICENSING.md) · [Legal](../../../legal/README.md) · [Contributing](../../../CONTRIBUTING.md) · [Security](../../../SECURITY.md) · [Inkcast README](../../../README.md)

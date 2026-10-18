# Secrets

How Inkcast reads, holds, rotates and hides its credentials. Code lives in `src/inkcast/libs/secrets/`.

Inkcast has five credentials of its own:

| Setting | Used by | What it is |
| --- | --- | --- |
| `INKCAST_SECRET_KEY` | every service except the Gateway | derives the key that seals platform credentials at rest, and the PKCE/nonce HMAC |
| `OIDC_CLIENT_SECRET` | IdentityService (`INKCAST_LOGIN=oidc`) | the client secret at your OpenID Connect provider |
| `GITHUB_CLIENT_SECRET` | IdentityService (`INKCAST_LOGIN=github`) | the GitHub OAuth app secret |
| `INKCAST_PASSWORD` | IdentityService (`INKCAST_LOGIN=password`) | the one shared password |
| `INKCAST_CACHE_PASSWORD` | every service except the Gateway, when `INKCAST_CACHE_URL` is set | the Redis/Valkey password (`AUTH`); never part of the URL |

Platform credentials (a DEV API key, a Medium token) never come from configuration: people type
them on the connect page and they are sealed into the database.

## Where a secret can come from

Every source implements one small interface (`Fetcher`). The process asks them in this order:

| Order | Source | Set with | Notes |
| --- | --- | --- | --- |
| 1 | a file named by `NAME_FILE` | `INKCAST_SECRET_KEY_FILE=/var/run/secrets/inkcast/secret-key` | **preferred**: the file is re-read, so a rotated Kubernetes Secret is picked up without a restart |
| 1 | an environment variable `NAME` | `INKCAST_SECRET_KEY=…` | works, but the posture check warns: env vars leak through `/proc`, child processes and `kubectl describe` |
| 2 | a directory, one file per secret | `INKCAST_SECRETS_DIR=/var/run/secrets/inkcast` | a Secret volume, the Vault Agent injector's output, or what External Secrets Operator wrote. `OIDC_CLIENT_SECRET` is found as `OIDC_CLIENT_SECRET`, `oidc_client_secret` or `oidc-client-secret`; `INKCAST_SECRET_KEY` also as `secret-key` |
| 3 | the AWS Workload Credentials Provider (Secrets Manager Agent) | `INKCAST_SECRETS_SOURCE=agent`, `INKCAST_AGENT_URL` (loopback only), `INKCAST_AGENT_TOKEN_FILE` | the SSRF token is read on every call and only ever sent to `localhost` |
| 3 | AWS Secrets Manager through boto3 | `INKCAST_SECRETS_SOURCE=aws` | boto3 is not in the image; the process says so and exits 78 |
| 3 | OpenBao / Vault | `INKCAST_SECRETS_SOURCE=vault` | reserved; use the agent injector or ESO with `dir` today |

The first source that has the secret wins, so a `NAME_FILE` always beats a remote back-end for that
name. `INKCAST_SECRETS_TTL` (300 s) and `INKCAST_SECRETS_MAX_STALE` (3600 s) tune the cache below.

A secret that **can't be read** (a dangling `NAME_FILE`, an agent that is down) is not fatal while the
configuration loads. `Config.validate(needs)` reports it only for the services that need that
credential, so a Gateway pod that receives the shared ConfigMap's `INKCAST_PASSWORD_FILE` but mounts no
secrets starts, and an IdentityService in the same situation exits 78 with the file name.

## Naming convention for remote ids

Local sources find a secret by its setting name. A secrets manager needs a *remote id*, rendered
from a pattern so that config says *which environment*, never a hand-typed path:

```
/{app}/{env}/{name}                  the default (preset app-env-name)
/inkcast/prod/secret-key             INKCAST_SECRET_KEY   in prod
/inkcast/prod/oidc-client-secret     OIDC_CLIENT_SECRET   in prod
```

| Token | Meaning | Set with |
| --- | --- | --- |
| `{env}` | required; one of `INKCAST_SECRETS_ALLOWED_ENVS` (default `dev,staging,prod`) | `INKCAST_SECRETS_ENV` |
| `{app}` | the application, default `inkcast` | `INKCAST_SECRETS_APP` |
| `{service}` | the running service's full name (`identity-service`), `inkcast` for `All` | automatic |
| `{name}` | the setting's slug: `INKCAST_SECRET_KEY` → `secret-key` | automatic |
| `{region}` | `AWS_REGION` | automatic |

Choose another order with `INKCAST_SECRETS_NAME_PATTERN`: a preset (`app-env-name`,
`app-env-service-name`, `env-app-name`, `env-app-service-name`) or your own pattern. A secret whose
name you don't choose gets an explicit id: `INKCAST_SECRET_ID_OIDC_CLIENT_SECRET=arn:aws:…`.

Rules enforced at startup (exit 78 with the reason): the pattern contains `{env}`; every value is one
path segment (letters, digits, `_+=.@-`, never `/`); the result is at most 512 characters with no `//`.
A name ending in `-` plus six characters is warned about: it looks like the suffix AWS adds to ARNs.

## Config files hold names, never values

`INKCAST_CONFIG_FILE` may point at a `.env`, `.yaml` or `.json` file with the same setting names as
the environment. Real environment variables win. The file is refused, and the process exits 78, if a
key looks like a credential (`INKCAST_SECRET_KEY: …`) or a value is a URL with a password in it. The
file may hold pointers: `INKCAST_SECRET_KEY_FILE`, `INKCAST_SECRETS_DIR`, `INKCAST_SECRET_ID_…`.

## In the process

`SecretsProvider` (one per process, `config.secrets`) is an in-memory, rotation-aware cache:

- values live only in memory, as `SecretValue`; `.reveal()` is called at the point of use, and
  `tests/test_architecture.py` keeps the list of files allowed to call it;
- TTL with ±10 % jitter, so a fleet does not re-read in lockstep; single-flight, so concurrent cold
  reads make one fetch;
- stale-on-error: if a re-read fails the last good value is served, for at most
  `INKCAST_SECRETS_MAX_STALE`, and the next try is 30 s away; a forced `refresh()` never serves stale;
- `refresh(name, reason)` is what a consumer calls when the other side rejects the credential (it was
  rotated), then retries once; `on_change(callback)` tells you when a value changed;
- every loaded value is registered with the **Redactor** before anything else can log it.

`SecretValue` prints as `SecretValue('***')` in `repr`, `str`, f-strings and `%s`; `json.dumps` and
`pickle` refuse it; `==` works against another `SecretValue` or a plain string, in constant time.

## Redaction

Two layers run on **every** log line, in JSON and text format, message and traceback alike:

1. patterns: Inkcast's own token formats (`ink_pat_…`, `ic_at_…`), `Bearer …`, `password=…`,
   `client_secret=…`, `code=…`;
2. registered values: the exact credentials this process loaded, in raw, JSON-escaped and URL-encoded
   form, wherever they appear: a message, an `extra=` field, a library's error text, a URL with a
   password (`postgres://u:[REDACTED]@host`).

`INKCAST_LOG_REVEAL_SECRETS=true` is the one deliberate exception: the DEBUG line "Secret consumed
(value revealed)" is written unmasked. It exists for labs, it is a `warn` in the posture table, and
production refuses to start with it.

## What a secret looks like in logs and on the metrics port

Never the value. A secret is described by its name, source, length and an HMAC fingerprint under a
per-process random key: two lines with the same fingerprint saw the same value, a changed fingerprint
means the value changed, and nothing can be recovered from it.

```
Secret INKCAST_SECRET_KEY: set (file:/var/run/secrets/inkcast/secret-key, fp=3f9c2a1b7d0e)   event=secrets.status
Secret OIDC_CLIENT_SECRET changed (ttl-expired, 3f9c… → 8a10…, source file:…)               event=secrets.changed
Could not re-read secret OIDC_CLIENT_SECRET (…); using the cached value (412s old)            event=secrets.fetch.stale
```

`GET /secrets` on the **metrics port** (9090, which the NetworkPolicy opens to the monitoring
namespace only) returns the provider mode, the TTL, `status()` for every secret asked for (name,
source, version, fingerprint, length, present, loaded_at, age, cache hits, fetches, changes, last
reason, last error, stale) and the posture check table. It is on the metrics port and not the public
port because secret names, sources and rotation state are operational data, not product API.

## Posture checks

`inkcast checks` prints the table; the launcher runs it after `Config.validate`. A `fail` exits 78,
a `warn` is logged once as `config.posture.warn`. `INKCAST_ENV=production` (the image default)
turns a weak `INKCAST_SECRET_KEY` and the reveal flag into failures.

| Check | pass | warn / fail |
| --- | --- | --- |
| `public-url-https` | https, or localhost | plain http elsewhere: **fail** |
| `secret-key-strength` | ≥ 32 random-looking chars | < 32: **fail**; placeholder words or < 16 distinct chars: warn (fail in production) |
| `no-reveal-flag` | unset | warn (fail in production) |
| `debug-flags` | none | `INKCAST_LOG_LEVEL=DEBUG` / `PYTHON*DEBUG` in production: warn |
| `log-format-json` | json | text in production: warn |
| `secret-files-protected` | mode without `o+r` | a world-readable `NAME_FILE`, `INKCAST_SECRETS_DIR` file or agent token: warn |
| `env-scrubbed` | no credential in env vars | a loaded value or credential-named variable in the environment: warn |
| `redaction-active` | values registered | nothing registered: warn |
| `non-root`, `core-dumps`, `forwarded-ips`, `login-method`, `database-protected`, `secrets-source` | see `inkcast checks` | |

## Adding a source

Subclass `Fetcher` in `libs/secrets/fetchers.py`, implement `fetch(name, force=False)` (return `None`
when you don't have the secret, raise `SecretFetchError` when you could not ask), optionally
`describe()` and `paths()`, register it in `BACKENDS`, and add a test with a fake transport like
`tests/test_secrets.py::FetcherTests` does for the agent.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)

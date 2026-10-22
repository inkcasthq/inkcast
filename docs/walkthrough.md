# The end-to-end walkthrough

A document that shows Inkcast being used, built and checked by CI on every change (issue #57, story F1.6.S4).
It drives the whole journey for real, in order, the way a person and their MCP client do it, and pictures every
step: a screenshot of the Inkcast page the person saw, or a rendered panel of the MCP tool call or REST request
the client made and the reply it got. It is also a test: a step whose reply lacks what it expects fails, a page
with a JavaScript error or a visible error is refused instead of pictured, and the run's exit status is non-zero
if anything failed.

## What it walks through

Both apps run in development mode against the fake DEV and Medium APIs and the fake Keycloak from `tests/fakes.py`
(OIDC sign-in through the fake's login form, which stands in for Keycloak's, and the user sync against its admin
API). The MCP side is the real Python SDK client, so the sign-in is the OAuth flow any MCP client performs:
discovery, dynamic registration, PKCE, the authorization URL opened in a browser, the code delivered to a real
local listener on the callback port, the token exchange.

1. Add Inkcast to an MCP client: discovery, registration, sign-in and consent in the browser, the callback, the
   token exchange, `initialize` and the tool list.
2. `inkcast_platforms` (nothing connected), `inkcast_connect` for DEV, the one-time connect page in the browser,
   `inkcast_platforms` again, `inkcast_destinations`.
3. `inkcast_publish` as a dry run, then as a draft on DEV; `inkcast_posts`.
4. `inkcast_publish` with `publish_at` an hour ahead; `inkcast_posts`; `inkcast_cancel`; `inkcast_posts`.
5. `inkcast_create_token`, then the token page in the browser (sign in, confirm, the token shown once and masked).
6. `POST /api/v1/stories:sync` with that token, exactly as the GitHub Action sends it (the file, its path, the
   repository id, an `Idempotency-Key`); `GET /api/v1/ledger`.
7. `GET /api/v1/insights/summary`, once the insights worker has folded the run's events.
8. The API reference pages, `/api` and `/api/redoc`.
9. What the fake DEV holds, read back for the integrity check.
10. Delete my account (E9.F9.5.S4): `inkcast_delete_account` (a one-time link, nothing deleted), the REST endpoint
    refusing the CI token, the fresh sign-in, Inkcast's page (what goes, what stays, the export coming soon), the
    provider's re-authentication and confirmation, its "deleted" page linking back, Inkcast's "account deleted"
    page; then both tokens answer 401, the erasure counter reads exactly 1 (`via="return"`, or `via="event"` when the
    sync read the DELETE_ACCOUNT event before the visit back), and the fake DEV still holds both articles: what was
    published stays.

The panels are plainly labelled as MCP tool calls or REST requests; they don't imitate any client's interface.
Generated credentials (OAuth tokens, the personal access token, the authorization code) are masked in every
picture and in `results.json`; the password (the fake Keycloak account's) is never written.

## Running it

```sh
scripts/dev/walkthrough/run_walk.sh --out /tmp/walkthrough
open /tmp/walkthrough/walkthrough.pdf
```

Needs Playwright with Chromium (`pip install playwright==1.56.0 && python -m playwright install chromium`), which
the test suite needs too. The password is read from `WALK_PASSWORD` if set and generated for the run otherwise;
it is never a command-line argument. Ports: `WALK_INKCAST_PORT` (8787), `WALK_FAKES_PORT` (8999),
`WALK_METRICS_PORT` (9090), `WALK_CALLBACK_PORT` (53682). About twenty seconds.

The command runs, in order:

| Step | Script | Leaves |
|---|---|---|
| the walk | `walk.py` | `NN-*.png`, `results.json` (every step: time, verdict, detail, the call and the reply), the process logs |
| integrity | `integrity_check.py` | `integrity.jsonl`: the DEV article matches the published story (title and body hash, re-rendered through the server's own DEV renderer); the ledger's `content_hash` equals the hash the server computes for the synced file; the cancelled schedule never reached DEV; no credential appears in full in any output file |
| environment | `env_facts.py` | `env.json`: git commit, Inkcast version, Python, Playwright, Chromium, OS |
| document | `build_doc.py` | `walkthrough.html` (self-contained), `walkthrough.pdf` (Chromium print), `summary.md` |

Every number in the document is read from those files; nothing is typed in. The document is built even when
the walk fails, so the evidence of what failed survives. An optional `findings.json` in the output directory (a
list of `{"title", "severity": "defect" | "note", "text"}`) becomes its Findings section.

## In CI

The `test` job of `ci.yml` runs it on Python 3.14 after the unit tests and uploads the output as the artifact
`walkthrough-<sha>` (every pull request). On `main`, `publish.yml`'s `captures` job pushes it to the
[`ci-captures`](https://github.com/inkcasthq/inkcast/tree/ci-captures) branch as `walkthrough/latest/`, next to
the dashboard captures; the branch README links `walkthrough.html` and `walkthrough.pdf`.

Unit tests for the helpers (masking, the panel's HTML, the counts, the credential scan, the one command's
argument handling, the CI wiring): `tests/test_walkthrough.py`.

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)

# Definition of done

A change is done when someone who wasn't in the room can see **what** was built, **why** it's safe, and **that** it works, without asking. This page applies to every story and every epic. Reviewers check it, and the [pull request template](../.github/pull_request_template.md) repeats it as a checklist.

## Story (every pull request)

| # | Area | Done means |
|---|---|---|
| 1 | **Design note** | `docs/epics/E<n>-…/stories.md` has the story with acceptance criteria and a short design note: what changes, why, and the alternative not taken. A story that changes a *flow* includes a sequence or flow diagram (Mermaid source committed; rendered with the Visualizer for review). A story that changes a *screen* includes an approved mockup **before** any UI code. |
| 2 | **Code** | Follows the [contributor map](contributor-map.md): shared code in `libs/`, service code in its own `services/<name>/`, no service importing another (enforced by `tests/test_architecture.py`). Every new file starts with the SPDX and copyright lines. No new dependency without a reason and a licence check in the PR. |
| 3 | **Tests** | Unit tests for logic, plus an end-to-end test through HTTP. Any external API gets a fake in `tests/fakes.py`. Data changes get a negative test proving one user can't touch another's data. The suite passes in **both** single-process mode and split mode (`tests/test_split_mode.py` and `GitPublishingSplit` run everything behind the Gateway). |
| 4 | **Security** | Every route and MCP tool checks a scope (`Authed(scope, …)` or `principal(scope)`). Every query is scoped to the caller. Secrets are never logged, never returned and never put in tool output (Redactor, `SecretValue`, hashes only). No platform calls happen inside a web request except when the person explicitly publishes now. Pages keep their CSP. Inputs are validated with pydantic models. |
| 5 | **API** | The contract (`libs/contracts/models.py`, `libs/contracts/openapi.py`) is updated first, with examples and the owning service. Errors are `application/problem+json`. Changes are backward compatible, or versioned. |
| 6 | **Operations** | Config comes from the environment (`*_FILE` for secrets), is documented, and is checked by `Config.validate(needs)`, so a bad value stops startup with exit 78. New paths emit structured logs and a metric. If a failure would be visible to users, it has an alert in the catalogue. |
| 7 | **CI** | All green: lint, tests on 3.11–3.13, manifests (`scripts/gen-manifests.py --check`), image scan, kind (single and split), CodeQL. Reviewed and squash-merged. Commits authored by the human owner. |
| 8 | **Docs** | User-facing behaviour is documented where users look (README, `docs/git-publishing.md` …). The epic's `implementation.md` is updated for the story. Acceptance criteria are ticked, with evidence (test names, screenshots). |

## Epic

| # | Done means |
|---|---|
| 1 | **`design.md`**: use cases, the system design with diagrams, data model, API sketch, risks, and lessons applied. UI epics add mockups (self-contained HTML plus PNG in `mockups/`), approved before build. |
| 2 | **ADRs** in `docs/adr/` for each significant decision, with options considered. |
| 3 | **`implementation.md`**: what was built (file map, seams used, data and migrations, configuration), how to roll out and roll back, and verification evidence. |
| 4 | **`review.md`**: an independent review by a reviewer who didn't write the code (a separate agent or person). It checks the code against the design, this definition of done and security. Every finding is fixed, or accepted with a reason. **The epic is not done while a P0 or P1 finding is open.** |
| 5 | **Shipped**: deployed to staging through GitOps by digest; demo walkthrough recorded (browser-walk captures); release notes written. |

## Severity for review findings

- **P0**: security hole, data loss, duplicate publishing, or broken build. Fix before merge.
- **P1**: wrong behaviour in a realistic case, or a missing guardrail a junior would trip over. Fix in the same epic.
- **P2**: polish, clarity, or a nice-to-have. Fix or file an issue.

---
<!-- inkcast-nav -->
[Docs](README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)

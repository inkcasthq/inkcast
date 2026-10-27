# ADR-0016: One mockup standard, and a frontend target per project

**Status:** accepted, 2026-10-26 (owner) · **Issue:** #235 · **Related:** ADR-0004 (web frontend), #227 (tokensave dashboard)

## Context

Mockups so far (E4, E10) were hand-written HTML pages, one per screen. Building the tokensave dashboard mockup (#227) showed where that breaks:

- **Colours drifted.** A dark version existed for one screen and not the others, so switching screens switched theme. New colours were added to one page and not the rest.
- **State didn't carry.** Each screen was its own page, so the theme, the date range and the filters were lost when you moved between them.
- **Nothing told the builder what the data was.** Numbers were typed into the markup, with no type or source.

A mockup that has to be re-read and re-interpreted by whoever builds the page is a sketch, not a contract.

The same work also needed a frontend for a page that lives inside a Rust binary, not in Inkcast's web app.

## Options considered

| Topic | Options | Chosen |
|---|---|---|
| Mockup form | Free-form HTML pages per screen; one app per mockup with shared state, tokens and typed data | **One app per mockup, to a written standard** |
| From mockup to page | Rebuild by hand from screenshots; third-party design-to-code tools (Figma Dev Mode, Anima, Locofy, Builder.io, v0); our own mapping | **Our own mapping.** The third-party tools target Figma and Next.js or React, and know neither our tokens nor our checks. |
| Embedded pages | React (the most used: 39.5% of developers, Stack Overflow survey 2025); Lit (web components); Preact (React's model, about 4 KB) | **Preact + TypeScript.** It has the same code style as Inkcast's React app, so components and the mapping are shared. It's small enough to embed. |

## Decision

1. **Every app mockup follows the house standard** in [docs/tooling/mockup-standard.md](../tooling/mockup-standard.md), the repo copy of the `build-mockup` and `mockup-to-page` skills. In short:
   - **One app:** each mockup is one interactive app with all its screens and shared state, and it declares its target framework.
   - **One token table:** every colour is a named token, defined for dark and light, and checked for contrast.
   - **Typed data:** sample data is a set of named datasets, each with a TypeScript type and its API source.
   - **Fixed sections:** every section is one of a fixed set of types, and each type becomes one component.
   - **Checked:** no mockup is published until every variant passes `scripts/mockups/check.py` at 390, 820 and 1440 px.
2. **Frontend targets, per project:**
   - **Inkcast's web app stays React + TypeScript** (ADR-0004, unchanged).
   - **Small pages embedded in a binary use Preact + TypeScript,** starting with the tokensave dashboard. Vite builds static files that the binary embeds, so the page works offline and Node is needed only to build.
3. **Themes:**
   - Every page has dark, light and auto (auto follows the system).
   - The default is a per-product choice: the tokensave dashboard defaults to dark.
   - Inkcast's own docs, diagrams and screenshots stay light-only.

## Consequences

- **Mechanical builds:** a page can be built mechanically from its mockup. The tokens become `tokens.css`, the typed datasets become API contracts and test fixtures, and the section types become one component each, shared between React and Preact by changing imports.
- **More up-front work:** mockups take a little more effort (a token table, typed data, the check run), but they no longer drift.
- **Existing mockups stay as they are:** the E4 and E10 mockups are not rewritten. They move to the standard when their screens are next designed or built.
- **Node in the fork's build:** the tokensave fork needs Node to build its dashboard. If the dashboard is proposed upstream, the maintainer may prefer otherwise; moving the components is mechanical under this standard.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)

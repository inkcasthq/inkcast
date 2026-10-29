# Mockup standard: from mockup to page

*Decided in [ADR-0016](../adr/0016-mockup-standard-and-frontend-targets.md) (issue #235). This is the repo copy of two Claude skills, `build-mockup` and `mockup-to-page`: change them together.*

Every app mockup is built to one standard, so the real page can be built from it mechanically:

- **Tokens:** every colour is a named token in one table.
- **State:** shared state is declared once, in one app.
- **Typed data:** sample data is a set of named datasets, each with its type and API source.
- **Sections:** every section is one of a fixed set of types, and each type becomes one component.

Frontend targets per project:

| Project | Framework |
|---|---|
| Inkcast's web app | React + TypeScript ([ADR-0004](../adr/0004-web-frontend.md)) |
| Small pages embedded in a binary, e.g. the tokensave dashboard (#227) | Preact + TypeScript |

## Tooling

| Script | What it does |
|---|---|
| `scripts/mockups/render.js` | Renders a design-canvas artboard (`.dc.html`) to one self-contained HTML page, for given props: `node scripts/mockups/render.js Main.dc.html out.html '{"theme":"dark","tab":"agents"}'` |
| `scripts/mockups/check.py` | Renders every variant (each combination of the artboard's enum tweaks: theme, tab, range...) and opens it at 390, 820 and 1440 px. It fails on a leftover placeholder, a page that scrolls sideways, or a background that stops short. `--export <dir>` then writes one HTML page and PNG per tab for the repo (light by default). |

Both need Node; `check.py` also needs Playwright with Chromium. A boolean attribute written as a hole (`checked="{{x}}"`, `selected`, `disabled`) is rendered only when its value is truthy, as the canvas does. `tests/test_mockup_tools.py` covers them.

## Building a mockup (`build-mockup`)

A mockup is the contract for the page built from it. Build it so `mockup-to-page` can translate it mechanically into the **target framework**: one source of colours, one source of state, typed data, a fixed set of section types. If a mockup breaks a rule below, fix the mockup, not the translation.

### 0. Declare the target first

Before any markup, settle and record it (in the issue, the canvas notes, and `data-props` as `"$target"`):

| Field | Values used today |
|---|---|
| `framework` | `react-ts`: Inkcast web app (React + TypeScript, ADR-0004). `preact-ts`: small pages embedded in a binary, e.g. the tokensave dashboard (Preact + TypeScript, owner's choice 2026-10-26). |
| `styling` | CSS custom properties from `tokens.css` (always) |
| `state` | URL query parameters for shareable state; browser storage for the theme |
| `data` | the API the page will call (REST paths) |

Both targets share one component model (function components, hooks, JSX), so one mockup can feed either. If a new project hasn't chosen, ask the owner before building.

### 1. Shape: one app, many frames

- **One interactive app artboard** (`Main.dc.html`) holds every tab or screen as an `<sc-if>` branch of one component, with all shared state in that component. Never one artboard per tab joined by links: their state drifts apart.
- **Preview frames** import the same app: one per tab (`<dc-import name="Main" tab="agents" theme="dark">`), a tablet frame (820 px) and a phone frame (390 px). They never copy markup.
- **A theme tokens sheet** (`Colors.dc.html`) lists every token with its light and dark value and contrast.
- Launch on the app; the canvas title says "sample data".

### 2. Colours: one token table

- `renderVals()` builds `T`, the token object for the active theme, from two tables (dark, light) defined once. Markup uses only `{{c.<token>}}`; no hex in markup outside the tables (except a wrapper frame's own background).
- Required names, which become the CSS variables one-to-one: `bg surface raised border track control text muted accent accentFg`, series tokens (e.g. `spent saved`), states `okFg okBg warnFg warnBg infoFg infoBg stopFg stopBg`, categories `cat.emerald cat.sky cat.amber cat.violet cat.coral cat.pink cat.teal cat.slate`.
- Category colours mark what a thing is (a tool, a role, a model) and keep their colour on every tab: map once (`TOOL`, `ROLE`, `MODEL`) and look up; never pick a colour inline. Icon tiles: the category colour tinted over `surface` (12% light, 16% dark).
- Contrast, checked by script before publishing: text ≥ 4.5:1, big numbers and chart marks ≥ 3:1, both themes. Two series differ in lightness, not hue alone; similar hues in one theme (e.g. amber vs coral) are adjusted until they read apart. Never colour alone: every badge carries its word.
- Modes: Dark, Light, Auto (`prefers-color-scheme`). The default is the owner's (tokensave dashboard: Dark). Inkcast's own product docs and screenshots stay light-only unless the owner says otherwise.

### 3. State: declared once, owned once

- Global state keys (e.g. `theme`, `tab`, `range`, `from`, `to`, filters) are initialised in the constructor, accepted as props (so preview frames open on a tab), and listed in `data-props` as enum tweaks.
- Above the constructor, a comment block declares each key: owner, type, default, persistence, e.g.
  `// state range: owner=App type='7'|'30'|'45'|'60'|'90'|'custom' default='30' url=?range=`
  `// state theme: owner=App type='dark'|'light'|'auto' default='dark' storage=theme`
- Local UI state (an open menu) is declared the same way on the section that owns it.
- Date ranges: presets (7, 30, 45, 60, 90 days) plus Custom (From/To bounded by today); the header states the range in words; every figure and list follows it.

### 4. Data: typed datasets with their source

- All sample data is returned from `renderVals()` as named datasets (`tiles`, `days`, `runs`, `repos`…), never typed into markup.
- Each dataset gets a TypeScript type and its source in comments right above it. These are the API contract and become `types.ts`:
  `// dataset runs: GET /api/runs?range&repo&tool → Run[]`
  `// type Run = { name: string; role: Role; tool: Tool; model: string; io: string; cost: string; state: 'finished'|'running'|'stopped'|'partial' }`
- Values that follow the range or filters are computed from them, so the mockup behaves like the page.
- Sample data is labelled ("Sample data · mockup"). Public repos get generic sample names: never a real person, a client, or the owner's other projects.

### 5. Sections: a fixed vocabulary = the component library

Every section is one of these, with `data-component="<type>"` and `data-dataset="<name>"` on its root. Each type becomes exactly one component in the target, so its props are the dataset plus labels:

| type | what it is | props |
|---|---|---|
| `app-header` | logo, sample pill, workspace, theme, filters, range, tabs | the global state |
| `kpi-tiles` | grid of label / big number / note | `tiles: Tile[]` |
| `time-series` | bars per day, legend, date ticks; scrolls in its card on a phone | `days: Day[], series: Series[], ticks: string[]` |
| `bar-list` | label, value, bar and category dot per row | `rows: BarRow[]` |
| `row-list` | rows that stack into cards on a phone (instead of wide tables) | `rows: T[], columns` |
| `heat-grid` | 2-D grid, sticky first column | `rows: HeatRow[], columns: string[]` |
| `card-grid` | cards with icon tile, status badge, details | `cards: Card[]` |
| `callout` | warning or tip, often with a `<code>` command | `tone, title, body, command?` |
| `stat-strip` | one card: a heading figure with its note, and a few labelled numbers beside it | `title, value, note, stats: Stat[]` |
| `segmented` | button group (theme, range, measure) with `aria-pressed` | `options, value, onChange` |
| `form` | labelled inputs, checkbox groups in a `fieldset` with a `legend`, and the buttons that send it to one endpoint | `value: <Request>, onChange, onSubmit` (the request type from the `// dataset`) |

Each section starts with an eyebrow (small upper-case letter-spaced `accent`) and an `h2`. A new type is added to this table in the same change.

### 6. Fits every screen

- Fluid root (`width: 100%`, `min-height: 100vh`), content max 1440 px, flex-wrap or `repeat(auto-fit, minmax(…))`, wide content in its own `overflow-x: auto` box, numbers in `clamp()`.
- Breakpoints (the page uses the same): phone < 640, tablet 640–1024, desktop, wide > 1440. Phone: one column; filters fold into a "Filters" button (helmet media query on `.ts-filters` / `.ts-filterbtn`, with `!important` since inline styles win otherwise); tabs and segmented groups scroll sideways; `row-list` rows stack. Touch targets ≥ 44 px.

### 7. Accessibility in the markup

Real `<button>`, `<a href>`, `<label>` + inputs; `aria-current` on the active tab; `aria-pressed` on segmented buttons; `aria-label` on icon-only controls; decorative SVG `aria-hidden`.

### 8. Review before publishing (always)

1. Render every tab × both themes × every range preset to static HTML with the converter (sc-for, sc-if, holes, `renderVals` with props).
2. In headless Chromium at 390, 820 and 1440 px, fail on any leftover `{{`, `sc-`, `undefined`, `NaN`; page width > viewport; background shorter than the viewport.
3. Screenshot a sample per theme and width and look: colours consistent across tabs, categories distinct, nothing cut off.
4. Fix, re-run, publish, and report what was checked.

### 9. Into the repo

When accepted, export each tab to self-contained HTML plus a PNG under the feature's `mockups/` folder (Inkcast: `docs/<area>/mockups/`, light screenshots unless the owner says), link them from the design doc with the `$target` table, and keep the canvas link in the issue.

## From mockup to page (`mockup-to-page`)

Translate a `build-mockup` mockup into the real page in the framework its `$target` declares. Tokens, state keys, typed datasets and section types map one-to-one. If the mockup doesn't follow `build-mockup` (no `$target`, hex in markup, untyped datasets, unknown section types), fix the mockup first and say so; don't guess.

### 0. Inputs

- The mockup: canvas files (`Main.dc.html`, tokens sheet) or the repo's `mockups/` HTML, plus its `$target`.
- The story with acceptance criteria, and the project's own workflow skill (Inkcast: `inkcast-delivery-workflow`): issue, branch, failing tests first, gates, PR, review.
- Use the libraries the project already has. Add a dependency only if the project's rules allow it, pinned.

### 1. Framework mapping

Both targets use function components, hooks and JSX, so components written for one move to the other by changing imports.

| Mockup | `react-ts` (Inkcast web app, ADR-0004) | `preact-ts` (embedded page, e.g. tokensave dashboard) |
|---|---|---|
| imports | `react` | `preact`, `preact/hooks`; `preact/compat` only for a React-only library |
| token tables | `tokens.css` with CSS variables, imported once | the same `tokens.css`, linked from `index.html` |
| theme state | context setting `data-theme` on `<html>`; storage in try/catch | the same, with `preact/hooks` (or `@preact/signals` if the project uses it) |
| URL state (`url=`) | the router's search params | `URLSearchParams` + `history.replaceState` in the app component |
| `owner=App` state | lifted to the app component, passed as props | the same |
| local section state | `useState` in that component | `useState` from `preact/hooks` |
| dataset + `// dataset` source | typed fetch hook per endpoint (the project's data library, else `fetch`), schema in `api/` | one typed `fetch` module per endpoint, a small `useResource` hook |
| each `data-component` type | one `.tsx` component in `components/` | one `.tsx` component in `components/` (`jsxImportSource: "preact"`) |
| `<sc-for>` | `.map()` with a stable `key` | the same |
| `<sc-if>` | conditional render | the same |
| handler props (`onClick="{{x}}"`) | `onClick` | `onClick` |
| `form` section | controlled inputs bound to the request type; the server's 422 shown on the field it names | the same |
| boolean attribute holes (`checked="{{x}}"`) | `checked={x}` | `checked={x}` |
| helmet media queries | media queries in the component's CSS (the project's way), at the mockup's breakpoints | one stylesheet or CSS modules via Vite, same breakpoints |
| build | the project's existing bundler | Vite + `@preact/preset-vite` to static files (one JS bundle, `tokens.css`, `index.html`) embedded in the binary at compile time; no CDN, works offline; Node only at build |
| tests | the project's runner + Testing Library | Vitest + `@testing-library/preact` |

Types come straight from the mockup's `// type` comments into `types.ts`, shared by the API layer and the components. Keep the mockup's markup structure and accessibility attributes (buttons, `aria-current`, `aria-pressed`, labels); charts get text values (a tooltip and a table view).

### 2. Tokens

- Generate `tokens.css` from the two tables: `:root[data-theme=dark]`, `:root[data-theme=light]`, and `:root[data-theme=auto]` resolved with `@media (prefers-color-scheme: …)`. Categories become `--cat-<name>`. Components use only `var(--token)`: no hex anywhere else.
- The page sets `data-theme` from the user's choice, remembered per browser, falling back to the configured default.
- A contrast test reads `tokens.css` and checks every pair the mockup's rules name, in both themes.

### 3. State

Each `url=` key is read on load and written on change (`history.replaceState`); invalid values fall back to the default. The theme uses storage, never the URL. Defaults come from the project's config (e.g. `dashboard.toml`), else the mockup's.

### 4. Data

- Each `// dataset` becomes an endpoint with a typed response schema. Write the schema first, with the mockup's sample data as the test fixture; the fixture renders the page like the mockup.
- Contract tests: the server's real response matches the schema. Everything that followed `range` or a filter is a server-side parameter, never filtered only in the browser.
- Add loading, empty and error states to every section, in the same section type (the mockup shows the full state).

### 5. Prove it matches the mockup

1. Render the page with the fixture at 390, 820 and 1440 px in both themes; compare to the mockup's screenshots with a pixel diff and a small tolerance. Larger differences are fixed, or listed and justified in the PR.
2. The `build-mockup` checks: no sideways scroll, background fills, no leftover placeholders.
3. Accessibility scan (axe or equivalent) with no serious issues; keyboard through every control.
4. All of it in CI (Playwright), with screenshots kept as artifacts.

### 6. Deliver

One PR per story in the project's flow: an issue, a branch, failing tests first, the smallest change, the gates, review. Sample data stays in tests only. The PR lists every place the page differs from the mockup and why, and links the mockup.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)

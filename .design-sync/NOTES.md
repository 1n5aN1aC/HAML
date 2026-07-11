# design-sync notes — HAML

HAML is a React (plain JS) + Vite **application**, not a component-library package.
There is no `dist/` component entry, no `package.json` `exports`/`main`, and no
`.d.ts` types. The sync runs the **package shape in synth-entry mode**.

## Build setup (why the config looks the way it does)
- **Custom entry `client/.ds-entry.mjs`** (gitignored): every component is a
  `export default function <Name>`, and a bare `export *` synth-entry drops
  default exports, so `window.HAML.<Name>` would be undefined. The custom entry
  does `export { default as <Name> } from './src/components/<Name>.jsx'` for all
  12. **When a component is added/removed/renamed, update BOTH `client/.ds-entry.mjs`
  AND `cfg.componentSrcMap`.**
- `cfg.componentSrcMap` pins all 12 src paths (needed because there's no `.d.ts`
  export list to discover from).
- `cfg.cssEntry = "src/app.css"` — the whole token system + component styles
  (732 lines, 51 CSS custom properties, 3 themes) live in that one file; it's
  appended to `_ds_bundle.css` and reachable from `styles.css`.
- `cfg.entry = "client/.ds-entry.mjs"` (cwd/repo-root-relative).
- Run: `node .ds-sync/package-build.mjs --config .design-sync/config.json \
  --node-modules ./client/node_modules --entry ./client/.ds-entry.mjs --out ./ds-bundle`
- Weak contracts: plain JS → no real prop types, so emitted `<Name>.d.ts` bodies
  are synthesized/loose. Expected for this repo. (A future `tsc`/JSDoc-types pass
  would sharpen them.)

## Preview patterns
- **IndexedDB seeding (ContactList, LoggingTab):** these read a live Dexie query
  over IndexedDB `haml`. The previews open a second `new Dexie('haml')` with the
  schema copied verbatim from `client/src/db.js` (see `previews/_fixtures.ts`
  `DB_STORES`), `clear()` + `bulkPut()` the fixture rows, `close()`, and gate the
  component mount on a `ready` flag so the first query returns rows. Works because
  the initial query reads IndexedDB directly (no cross-instance liveQuery
  notification needed).
- **ContactModal (overlay):** its `.modal-backdrop` is `position: fixed`. The
  preview wraps it in `<div style={{transform:'translateZ(0)', minHeight:560}}>`
  so the wrapper becomes the containing block for the fixed layer — it centers
  inside the card and measures real height (fixes an otherwise-flagged
  `RENDER_THIN`). Carded `cardMode: single`, viewport `820x620`.
- Wide bars (**TopBar, StatusBar, ContactEntryForm**): `cardMode: column`.
- Shared realistic fixtures in `previews/_fixtures.ts` (Field Day template config,
  contacts, presence stations, chat) — not a component, so never built as a preview.

## Known render warns (re-syncs: check new warns against this list)
- **RadioTab / StatisticsTab** render `[RENDER_THIN]` — they are genuine
  "— coming soon" placeholders (the real components are a single line). Not a
  defect; graded good as placeholders.
- **TopBar** brand icon: `<img src="/favicon.svg">` 404s in the preview server
  (absolute path), so a tiny broken-image glyph shows next to "HAML". Cosmetic;
  the app serves the favicon at runtime.

## Verification
- Playwright `1.61.1` + Chromium build `1228` installed into `.ds-sync/node_modules`
  (~200MB, one-time). Render check ran clean, 12/12.

## Re-sync risks
- `previews/_fixtures.ts` inlines a Field Day-style template config and Contact
  row shape. If the app's Contact columns or Template config shape change
  (`client/src/db.js`, `server/templates/*.json`, `contact-validation.js`), update
  the fixtures or ContactList/LoggingTab/ContactModal/EntryForm previews will
  render stale/wrong data.
- The custom entry + `componentSrcMap` must track the component set (see Build setup).
- No `.d.ts` in the repo: prop contracts are synthesized. A component whose props
  change won't surface a contract diff — the previews are the real guard.
- The Dexie seed schema in `_fixtures.ts` is copied from `client/src/db.js`
  (`db.version(1).stores(...)`). If the app bumps the Dexie version/schema, mirror
  it here.

# Building with HAML

HAML is a React (plain JavaScript) app for logging amateur-radio contacts during
an event (Field Day, POTA, contests). These components are **application parts** —
bars, panels, forms, a modal, and tab pages — composed by props into screens. They
are not a generic widget kit; build HAML-style logging UIs by composing them.

## Setup & theming
- **No provider or wrapper is required for styling.** Components render fully
  styled as soon as `styles.css` is present. All style comes from semantic CSS
  classes + CSS custom properties — there is no theme context to wrap in.
- **Theming is a root attribute, not context:** set
  `document.documentElement.dataset.theme = 'dark'`. The base `:root` **is** the
  default **Light** theme; `[data-theme="dark"]`, `[data-theme="blue"]`, and
  `[data-theme="purple"]` override only the tokens that differ. Make new UI
  theme-aware by using the tokens below, never hard-coded colors.
- Components are **prop-driven**: data (session, config, contacts, stations, chat)
  is passed in; mutations flow out through callbacks (`onSession`, `onSelect`,
  `onChatSend`, `onClose`, …). `ContactList` and `LoggingTab` additionally read a
  local Dexie/IndexedDB store named `haml`.

## Styling idiom: semantic classes + CSS variables
This is **not** a utility-class system and there are **no style props**. Style with
the design system's own class names and `var(--…)` tokens. Read `styles.css` /
`_ds_bundle.css` before adding UI.

Tokens (`var(--…)`):
- Surfaces/text: `--bg-page`, `--panel-bg`, `--panel-alt-bg`, `--text`,
  `--text-muted`, `--text-faint`, `--text-label`, `--border`, `--border-subtle`
- Accent/actions: `--accent`, `--accent-text`,
  `--accent-disabled`, `--row-hover-bg`, `--focus-ring`
- Chrome: `--topbar-bg`, `--topbar-text`, `--topbar-title`, `--tab-bg`,
  `--tab-active-bg`, `--statusbar-bg`, `--statusbar-text`
- Signal colors (constant meaning across all themes): `--danger`, `--success`,
  `--warning`, `--conn-down`; presence-freshness ramp `--fresh` / `--stale` /
  `--old`; field validation `--validation-ok` / `--validation-bad`
- Layout: `--panel-gap`, `--panel-radius`

Class vocabulary: layout `app`, `panes`, `left-pane`, `right-pane`, `tab-page`;
chrome `top-bar`, `status-bar`; panels `contact-list`, `entry-form`,
`stations-panel`, `chat-panel`, `modal` / `modal-backdrop`; buttons `btn-primary`,
`btn-danger`, `btn-secondary`; helpers `cs` (callsign, monospace), `placeholder`
(empty-state text), `v-ok` / `v-bad` (field-validation borders).

## Where the truth lives
- `styles.css` → `_ds_bundle.css`: every token and component style (one file).
- `components/<Name>/<Name>.prompt.md`: each component's props + usage examples.

## Idiomatic snippet
```jsx
import { StatusBar, ContactList, StationsPanel } from 'haml-client'

document.documentElement.dataset.theme = 'dark' // Light | dark | blue | purple

function LogView({ session, setSession, config, stations, me, openEditor }) {
  return (
    <div className="app">
      <StatusBar session={session} onSession={setSession} config={config} />
      <main className="panes">
        <section className="left-pane">
          <ContactList config={config} onSelect={openEditor} />
        </section>
        <aside
          className="right-pane"
          style={{ background: 'var(--panel-bg)', borderRadius: 'var(--panel-radius)' }}
        >
          <StationsPanel stations={stations} clientUuid={me} conflictUuids={new Set()} bands={config.bands} />
        </aside>
      </main>
    </div>
  )
}
```

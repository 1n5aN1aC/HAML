// Event-wide contact list: most recent ~50, newest first, with per-row
// sync-state dot. Live view over Dexie — new logs appear as they're written.
// The 🔍 in the header filters the whole local log (not just the visible 50).
import { useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { db } from '../../db.js'

const DISPLAY_LIMIT = 50

// Every space-separated token must match somewhere among the visible columns
// (not timestamps), case-insensitive — so "w7 ssb" finds W7ABC on SSB.
function matches(c, tokens) {
  const values = [c.remote_callsign, c.band, c.mode, c.operator_callsign, ...Object.values(c.fields ?? {})]
    .map((v) => String(v ?? '').toLowerCase())
  return tokens.every((t) => values.some((v) => v.includes(t)))
}

function formatTime(iso) {
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toISOString().slice(5, 16).replace('T', ' ')
}

function formatLocalTime(iso) {
  const d = new Date(iso)
  if (isNaN(d)) return iso
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  return `${h}:${m}`
}

export default function ContactList({ config, onSelect }) {
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState('')
  const needle = query.trim().toLowerCase()
  const tokens = needle ? needle.split(/\s+/) : []

  const contacts = useLiveQuery(
    () =>
      db.contacts
        .orderBy('qso_at')
        .reverse()
        .filter((c) => !c.deleted && (tokens.length === 0 || matches(c, tokens)))
        .limit(DISPLAY_LIMIT)
        .toArray(),
    [needle],
  )

  // Toggling closed always clears — never a hidden active filter.
  function toggleSearch() {
    if (searchOpen) setQuery('')
    setSearchOpen(!searchOpen)
  }

  // template's contact_list picks and orders the custom columns; absent = all
  const fields = config.contact_list
    ? config.contact_list
        .map((name) => config.fields.find((f) => f.name === name))
        .filter(Boolean)
    : config.fields
  if (!contacts) return <div className="contact-list" />

  return (
    <div className="contact-list">
      <table>
        <thead>
          <tr>
            <th className="sync-col">
              {/* preventDefault keeps the input from blur-closing first, which
                  would make this click reopen the box instead of closing it */}
              <button
                type="button"
                className="search-toggle"
                title="Search contacts"
                onMouseDown={(e) => e.preventDefault()}
                onClick={toggleSearch}
              >
                🔍
              </button>
            </th>
            <th>UTC</th>
            <th>Local</th>
            <th>Call</th>
            <th>Band</th>
            <th>Mode</th>
            {fields.map((f) => (
              <th key={f.name}>{f.label}</th>
            ))}
            <th>Op</th>
          </tr>
          {searchOpen && (
            <tr className="search-row">
              <th colSpan={7 + fields.length}>
                <input
                  autoFocus
                  value={query}
                  placeholder="Search…"
                  onBlur={() => {
                    if (!query.trim()) setSearchOpen(false)
                  }}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') {
                      setSearchOpen(false)
                      setQuery('')
                    }
                  }}
                />
              </th>
            </tr>
          )}
        </thead>
        <tbody>
          {contacts.map((c) => (
            <tr key={c.uuid} className="row-click" onClick={() => onSelect(c)}>
              <td className="sync-col">
                <span
                  className={c.sync_state === 'synced' ? 'dot dot-synced' : 'dot dot-pending'}
                  title={c.sync_state === 'synced' ? 'Synced to server' : 'Not yet synced'}
                />
              </td>
              <td>{formatTime(c.qso_at)}</td>
              <td>{formatLocalTime(c.qso_at)}</td>
              <td className="cs">{c.remote_callsign}</td>
              <td>{c.band}</td>
              <td>{c.mode}</td>
              {fields.map((f) => (
                <td key={f.name}>{c.fields[f.name] ?? ''}</td>
              ))}
              <td>{c.operator_callsign}</td>
            </tr>
          ))}
          {contacts.length === 0 && (
            <tr>
              <td className="empty" colSpan={7 + fields.length}>
                {needle ? 'No matching contacts' : 'No contacts logged yet'}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

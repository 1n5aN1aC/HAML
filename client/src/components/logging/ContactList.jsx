// Event-wide contact list: most recent ~50, newest first, with per-row
// sync-state dot. Live view over Dexie — new logs appear as they're written.
import { useLiveQuery } from 'dexie-react-hooks'
import { db } from '../../db.js'

const DISPLAY_LIMIT = 50

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
  const contacts = useLiveQuery(
    () =>
      db.contacts
        .orderBy('qso_at')
        .reverse()
        .filter((c) => !c.deleted)
        .limit(DISPLAY_LIMIT)
        .toArray(),
    [],
  )

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
            <th className="sync-col" title="Sync state" />
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
                No contacts logged yet
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

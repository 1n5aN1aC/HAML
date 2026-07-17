// Admin tab: password-gated event/template management (ADR-0004).
// The password is held in component state only — never persisted — and every
// admin call sends it as X-Admin-Password; the server is the real gate.
import { useState } from 'react'
import {
  getEvent,
  postContact,
  adminListTemplates,
  adminGetTemplate,
  adminDeleteTemplate,
  adminListEvents,
  adminCreateEvent,
  adminActivateEvent,
  adminDeleteEvent,
  adminBackup,
  adminClearChat,
  adminLookupCacheStats,
  adminClearLookupCache,
} from '../../api.js'
import { generateTestContacts } from '../../admin-test-data.js'
import AdminTemplateEditor from './AdminTemplateEditor.jsx'

const EMPTY_FORM = {
  name: '',
  station_callsign: '',
  template: '',
  local_exchange: '',
  latitude: '',
  longitude: '',
}

// onEventChange (optional): called after this tab creates or activates an
// event. The no-event screen uses it to re-boot — there's no WebSocket
// running in that state to deliver the server's event-switch signal.
export default function AdminTab({ onEventChange }) {
  const [password, setPassword] = useState('')
  const [unlocked, setUnlocked] = useState(false)
  const [unlockError, setUnlockError] = useState('')
  const [templates, setTemplates] = useState([])
  const [events, setEvents] = useState([])
  const [form, setForm] = useState(EMPTY_FORM)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // null, or { id, template } — id/template are null when creating from scratch
  const [editing, setEditing] = useState(null)
  // null until first load, then { ok, not_found, error } counts
  const [cacheStats, setCacheStats] = useState(null)

  async function refresh(pw = password) {
    const [{ events }, { templates }, stats] = await Promise.all([
      adminListEvents(pw),
      adminListTemplates(pw),
      adminLookupCacheStats(pw),
    ])
    setEvents(events)
    setTemplates(templates)
    setCacheStats(stats)
  }

  async function unlock(e) {
    e.preventDefault()
    setUnlockError('')
    try {
      await refresh(password)
      setUnlocked(true)
      setError('')
      setNotice('')
    } catch (err) {
      setUnlockError(err.status === 401 ? 'Wrong password.' : err.message)
    }
  }

  // Run an admin action; a 401 mid-session (password changed on the server)
  // drops back to the locked view instead of showing a dead GUI.
  async function run(action) {
    setError('')
    setNotice('')
    try {
      await action()
    } catch (err) {
      if (err.status === 401) {
        setUnlocked(false)
        setPassword('')
        setUnlockError('Password no longer accepted — unlock again.')
      } else {
        setError(err.message)
      }
    }
  }

  function deleteTemplate(t) {
    if (!window.confirm(`Delete template "${t.name}"? This cannot be undone.`))
      return
    run(async () => {
      await adminDeleteTemplate(password, t.id)
      await refresh()
    })
  }

  function editTemplate(id) {
    run(async () => {
      const template = await adminGetTemplate(password, id)
      setEditing({ id, template })
    })
  }

  function closeEditor(saved) {
    setEditing(null)
    if (saved) run(refresh)
  }

  function activateEvent(event) {
    if (
      !window.confirm(
        `Activate "${event.name}"? All connected operators will be switched to it.`,
      )
    )
      return
    run(async () => {
      await adminActivateEvent(password, event.event_uuid)
      await refresh()
      onEventChange?.()
    })
  }

  function deleteEvent(event) {
    if (
      !window.confirm(
        `Delete "${event.name}"? Its entire log will be permanently removed.`,
      )
    )
      return
    run(async () => {
      await adminDeleteEvent(password, event.event_uuid)
      await refresh()
    })
  }

  function createEvent(e) {
    e.preventDefault()
    if (
      !window.confirm(
        `Create and activate "${form.name}"? All connected operators will be switched to it.`,
      )
    )
      return
    run(async () => {
      await adminCreateEvent(password, form)
      setForm(EMPTY_FORM)
      await refresh()
      onEventChange?.()
    })
  }

  function backup() {
    run(async () => {
      const { backup } = await adminBackup(password)
      setNotice(`Backup written: ${backup}`)
    })
  }

  // Generates fake contacts client-side from the active event's config and
  // pushes them through the normal contact endpoint, so they sync to every
  // operator like real traffic. Marked TEST/TT so they're easy to spot.
  function addTestContacts() {
    if (!window.confirm("Add 25 random test contacts to the active event's log?"))
      return
    run(async () => {
      const { config, name } = await getEvent()
      const contacts = generateTestContacts(config, 25)
      for (const contact of contacts) await postContact(contact)
      setNotice(`Added ${contacts.length} test contacts to "${name}".`)
    })
  }

  function clearAllChat() {
    run(async () => {
      await adminClearChat(password)
      setNotice('All chat deleted.')
    })
  }

  function clearLookupCache() {
    if (!window.confirm('Clear all cached callsign lookups?')) return
    run(async () => {
      const { deleted } = await adminClearLookupCache(password)
      setCacheStats(await adminLookupCacheStats(password))
      setNotice(`Lookup cache cleared (${deleted} rows).`)
    })
  }

  if (!unlocked) {
    return (
      <div className="tab-page admin-page">
        <form className="admin-unlock" onSubmit={unlock}>
          <label>
            Admin password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
            />
          </label>
          <button type="submit">Unlock</button>
          {unlockError && <p className="admin-error">{unlockError}</p>}
        </form>
      </div>
    )
  }

  if (editing) {
    return (
      <AdminTemplateEditor
        password={password}
        templateId={editing.id}
        initial={editing.template}
        existingIds={templates.map((t) => t.id)}
        onDone={closeEditor}
      />
    )
  }

  const formComplete =
    form.name.trim() && form.station_callsign.trim() && form.template

  return (
    <div className="tab-page admin-page">
      {error && <p className="admin-error">{error}</p>}
      {notice && <p className="admin-notice">{notice}</p>}

      <section className="admin-section">
        <h2>Events</h2>
        {events.length === 0 && <p className="placeholder">No events yet.</p>}
        <table className="admin-list">
          <tbody>
            {events.map((event) => (
              <tr key={event.event_uuid}>
                <td className="admin-name">
                  {event.name}
                  {event.active && <span className="admin-badge">active</span>}
                </td>
                <td>{event.station_callsign}</td>
                <td>{event.template_name}</td>
                <td className="admin-date">
                  {event.created_at && new Date(event.created_at).toLocaleDateString()}
                </td>
                <td className="admin-actions">
                  {event.active ? (
                    <button onClick={backup}>Backup</button>
                  ) : (
                    <>
                      <button onClick={() => activateEvent(event)}>Activate</button>
                      <button className="btn-danger" onClick={() => deleteEvent(event)}>
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="admin-section">
        <h2>Create event</h2>
        <form className="admin-create" onSubmit={createEvent}>
          <label>
            Name
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <label>
            Station callsign
            <input
              className="cs"
              value={form.station_callsign}
              onChange={(e) =>
                setForm({ ...form, station_callsign: e.target.value })
              }
            />
          </label>
          <label>
            Template
            <select
              value={form.template}
              onChange={(e) => setForm({ ...form, template: e.target.value })}
            >
              <option value="">— pick —</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Exchange
            <input
              value={form.local_exchange}
              placeholder="W7XYZ 6A OR"
              onChange={(e) =>
                setForm({ ...form, local_exchange: e.target.value })
              }
            />
          </label>
          <label>
            Latitude
            <input
              className="admin-coord"
              value={form.latitude}
              placeholder="45.0"
              onChange={(e) => setForm({ ...form, latitude: e.target.value })}
            />
          </label>
          <label>
            Longitude
            <input
              className="admin-coord"
              value={form.longitude}
              placeholder="-123.0"
              onChange={(e) => setForm({ ...form, longitude: e.target.value })}
            />
          </label>
          <button type="submit" disabled={!formComplete}>
            Create &amp; activate
          </button>
        </form>
      </section>

      <section className="admin-section">
        <h2>Templates</h2>
        {templates.length === 0 && <p className="placeholder">No templates.</p>}
        <table className="admin-list">
          <tbody>
            {templates.map((t) => (
              <tr key={t.id}>
                <td className="admin-name">{t.name}</td>
                <td className="admin-id">{t.id}</td>
                <td className="admin-actions">
                  <button onClick={() => editTemplate(t.id)}>Edit</button>
                  <button className="btn-danger" onClick={() => deleteTemplate(t)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button
          className="admin-new-template"
          onClick={() => setEditing({ id: null, template: null })}
        >
          New template
        </button>
      </section>

      <section className="admin-section">
        <h2>Lookup cache</h2>
        <div className="admin-maintenance">
          <span>
            {cacheStats
              ? `${cacheStats.ok} found · ${cacheStats.not_found} not found · ${cacheStats.error} errors`
              : '…'}
          </span>
          <button className="btn-danger" onClick={clearLookupCache}>
            Clear
          </button>
        </div>
      </section>

      <section className="admin-section">
        <h2>Maintenance</h2>
        <div className="admin-maintenance">
          <button onClick={addTestContacts}>Add 25 test contacts</button>
          <button className="btn-danger" onClick={clearAllChat}>
            Delete all chat
          </button>
        </div>
      </section>
    </div>
  )
}

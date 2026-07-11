// Admin tab: password-gated event/template management (ADR-0004).
// The password is held in component state only — never persisted — and every
// admin call sends it as X-Admin-Password; the server is the real gate.
import { useState } from 'react'
import {
  adminListTemplates,
  adminDeleteTemplate,
  adminListEvents,
  adminCreateEvent,
  adminActivateEvent,
  adminBackup,
  adminClearChat,
} from '../api.js'

const EMPTY_FORM = { name: '', station_callsign: '', template: '' }

export default function AdminTab() {
  const [password, setPassword] = useState('')
  const [unlocked, setUnlocked] = useState(false)
  const [unlockError, setUnlockError] = useState('')
  const [templates, setTemplates] = useState([])
  const [events, setEvents] = useState([])
  const [form, setForm] = useState(EMPTY_FORM)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function refresh(pw = password) {
    const [{ events }, { templates }] = await Promise.all([
      adminListEvents(pw),
      adminListTemplates(pw),
    ])
    setEvents(events)
    setTemplates(templates)
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

  function deleteTemplate(id) {
    run(async () => {
      await adminDeleteTemplate(password, id)
      await refresh()
    })
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
    })
  }

  function backup() {
    run(async () => {
      const { backup } = await adminBackup(password)
      setNotice(`Backup written: ${backup}`)
    })
  }

  function clearAllChat() {
    run(async () => {
      await adminClearChat(password)
      setNotice('All chat deleted.')
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
                  {!event.active && (
                    <button onClick={() => activateEvent(event)}>Activate</button>
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
                  <button className="btn-danger" onClick={() => deleteTemplate(t.id)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="admin-section">
        <h2>Maintenance</h2>
        <div className="admin-maintenance">
          <button onClick={backup}>Backup active event</button>
          <button className="btn-danger" onClick={clearAllChat}>
            Delete all chat
          </button>
        </div>
      </section>
    </div>
  )
}

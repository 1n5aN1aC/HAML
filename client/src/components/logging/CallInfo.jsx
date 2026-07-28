// Lookup detail for the callsign currently in the entry form. Fills the left
// pane below the form and renders whatever the server's canonical lookup record
// (server/lookup_record.py — every key always present, null when unknown) holds.
// ContactEntryForm owns the record and hands it up; this panel is display only,
// so showing more of the lookup is an edit here and nowhere else.
// `record` is null before the first hit and on every callsign edit/submit.
// `here` is the event's operating position (event.config.location)
import { heading } from '../../map-math.js'

// Place, then how to point at it: "Oregon (16mi / 95°)".
// The record carries `country` and a `distance` in km when the event has a location;
// The heading is computed from `here` (the event's operating position) to the record's coordinates.
// Each half of the parenthetical is independent — either can be missing, and
// the parenthetical is dropped entirely when both are.
function locationLine(record, here) {
  const place = record?.pota_park || record?.country
  if (!place) return ''
  const parts = []
  if (record.distance != null) {
    parts.push(`${Math.round(record.distance * 0.621371).toLocaleString()}mi`)
  }
  const deg = heading(here, record)
  if (deg != null) parts.push(`${deg}°`)
  return parts.length ? `${place} (${parts.join(' / ')})` : place
}

// Name + license class. `license_class` is lowercased, so Capitalize it for display
// Either half may be missing — the class alone still names something useful.
function operatorLine(record) {
  const name = record?.name
  const cls = record?.license_class?.replace(/\b\w/g, c => c.toUpperCase())
  if (name && cls) return `${name} (${cls})`
  return name || cls || ''
}

export default function CallInfo({ record, here }) {
  const location = locationLine(record, here)
  const operator = operatorLine(record)

  return (
    <div className="call-info">
      <section className="call-info-group">
        <h2>Operator Location</h2>
        {location
          ? <p className="call-country">{location}</p>
          : <p className="placeholder">—</p>}
      </section>
      <section className="call-info-group">
        <h2>Operator Info</h2>
        {operator
          ? <p className="call-name">{operator}</p>
          : <p className="placeholder">—</p>}
      </section>
    </div>
  )
}

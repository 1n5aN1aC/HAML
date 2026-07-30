// Lookup detail for the callsign currently in the entry form. Renders two cards
// below the form in the left pane — .info-operator (who they are) and
// .info-ultracheck (partial matches) — from the server's canonical lookup record
// (server/lookup_record.py — every key always present, null when unknown).
// ContactEntryForm owns the record and hands it up; this panel is display only,
// so showing more of the lookup is an edit here and nowhere else.
// `record` is null before the first hit and on every callsign edit/submit.
// `here` is the event's operating position (event.config.location)
// `ultracheck` rides the same lookup but on a longer lifecycle — see Ultracheck below.
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

// How each source's value displays inline. LoTW/Club Log timestamps are truncated
// to year so more calls fit on the line; full value is available via tooltip.
const WHOLE = (v) => String(v)
// Truncate timestamps to year.
const YEAR = (v) => String(v).slice(0, 4)

// Display order = priority ranking (later sources clip off the bottom).
const ULTRACHECK_SOURCES = [
  ['fd', 'FD', WHOLE],
  ['wfd', 'WFD', WHOLE],
  ['pota_hunter', 'POTA Hunter', WHOLE],
  ['pota_activator', 'POTA Activator', WHOLE],
  ['lotw', 'LoTW', YEAR],
  ['clublog', 'Club Log', YEAR],
  ['scp', 'SCP', WHOLE],
]

// Partial-callsign matches from contest/activity datasets. `ultracheck` is the
// form's `{ data, stale }` pair (or null); stale matches render dimmed.
function Ultracheck({ ultracheck }) {
  const data = ultracheck?.data
  if (!data?.available) return null
  const groups = ULTRACHECK_SOURCES
    .map(([key, label, format]) => [label, data.sources?.[key]?.matches ?? [], format])
    .filter(([, matches]) => matches.length)
  if (!groups.length) return null

  // Early returns also skip the card frame — no empty box when there's nothing to show.
  return (
    <div className="call-info info-ultracheck">
      <section className={`call-info-group ultracheck${ultracheck.stale ? ' stale' : ''}`}>
        <h2>Ultracheck</h2>
        {groups.map(([label, matches, format]) => (
          <div className="ultracheck-source" key={label}>
            <span className="ultracheck-label">{label}</span>
            <p className="ultracheck-calls">
              {matches.map((m) => {
                const full = m.value != null ? String(m.value) : null
                const shown = full != null ? format(m.value) : null
                return (
                  <span
                    key={m.callsign}
                    className="ultracheck-match"
                    // Tooltip with full value when the inline display is lossy (e.g. year-truncated dates).
                    title={full != null && shown !== full ? full : undefined}
                  >
                    {/* Bold the call if it's an exact match against the query. */}
                    <span className={m.callsign === data.query ? 'exact' : undefined}>
                      {m.callsign}
                    </span>
                    {shown != null && <span className="ultracheck-value"> ({shown})</span>}
                  </span>
                )
              })}
            </p>
          </div>
        ))}
      </section>
    </div>
  )
}

export default function CallInfo({ record, here, ultracheck }) {
  const location = locationLine(record, here)
  const operator = operatorLine(record)

  // Two cards sharing .call-info frame, diverging via .info-operator / .info-ultracheck.
  return (
    <>
      <div className="call-info info-operator">
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
      <Ultracheck ultracheck={ultracheck} />
    </>
  )
}

// Lookup detail for the callsign currently in the entry form. Fills the left
// pane below the form and renders whatever the server's canonical lookup record
// (server/lookup_record.py — every key always present, null when unknown) holds.
// ContactEntryForm owns the record and hands it up; this panel is display only,
// so showing more of the lookup is an edit here and nowhere else.
// `record` is null before the first hit and on every callsign edit/submit.

// Country + distance. The record carries `country` (always set on a hit) and a
// request-time `distance` in km when the event has a location: country + miles
// when both are present, country alone when the event has no location.
function locationLine(record) {
  if (!record?.country) return ''
  const km = record.distance
  if (km == null) return record.country
  const mi = Math.round(km * 0.621371)
  return `${record.country} (${mi.toLocaleString()} mi)`
}

export default function CallInfo({ record }) {
  const location = locationLine(record)

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
        {record?.name
          ? <p className="call-name">{record.name}</p>
          : <p className="placeholder">—</p>}
      </section>
    </div>
  )
}

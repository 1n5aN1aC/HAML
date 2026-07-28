// Spherical-earth geometry for the map and the lookup readouts. Pure math over
// {latitude, longitude} pairs in degrees — no React, no records, no wire shape,
// so the map work landing here later (great-circle polylines between the same
// two points) extends this file instead of reimplementing the trig.
//
// The server computes `distance` its own way (server/lookup_postprocess.py) so
// that a cached row never carries a value measured from a stale event position.
// Heading is request-relative in exactly the same sense, but the client already
// holds both endpoints — the operating position rides in event.config.location —
// so it is derived here rather than widening the wire.

const RAD = Math.PI / 180

// A coordinate pair as radians, or null when either half can't be used.
// Same gate the server applies (`_valid_coord`): missing, non-finite or
// out-of-range answers null instead of producing a nonsense heading.
function radians(point) {
  const lat = point?.latitude
  const lon = point?.longitude
  if (typeof lat !== 'number' || typeof lon !== 'number') return null
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null
  return { lat: lat * RAD, lon: lon * RAD }
}

// Initial great-circle heading from `from` to `to`, in degrees clockwise from
// true north (0-360), rounded to a whole degree. Null when either point is
// unusable.
//
// This is the INITIAL bearing: the heading changes along a great-circle path,
// so a long path ends on a different course than it started. That start heading
// is the number a rotator wants, which is why it is the one reported.
//
// Note it is true north, not magnetic — declination is a local correction this
// deliberately does not apply.
export function heading(from, to) {
  const a = radians(from)
  const b = radians(to)
  if (a === null || b === null) return null
  const dLon = b.lon - a.lon
  const y = Math.sin(dLon) * Math.cos(b.lat)
  const x = Math.cos(a.lat) * Math.sin(b.lat)
    - Math.sin(a.lat) * Math.cos(b.lat) * Math.cos(dLon)
  // atan2 answers -PI..PI; shift into a compass 0-360 before rounding.
  // 360 itself rounds back to 0 so due north has one spelling, not two.
  return Math.round((Math.atan2(y, x) / RAD + 360) % 360) % 360
}
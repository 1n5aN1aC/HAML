// QSO rate graph for the Stats page (stats-top pane): a stacked bar chart
// of QSO/h over the contest span (first contact → last contact), one stack
// segment per mode. Hand-rolled inline SVG — no charting dependency (ADR-0006),
// the first SVG we draw ourselves in the client.
// Live view over Dexie, same pattern as StatsPanel. No 60s tick: the
// x-domain runs first→last contact, not "now", so nothing on screen changes
// with wall-clock time — useLiveQuery already re-renders on every logged/edited
// contact (which also moves the last-contact edge).

import { useCallback, useRef, useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { db } from '../../db.js'

// Bin-width ladder (minutes). We pick the smallest step that keeps the bar
// count at or under 50 across the span, so short events get fine detail and
// long ones stay legible.
const BIN_LADDER = [5, 10, 15, 30, 60, 120, 240, 360, 720, 1440]
function pickBinMinutes(spanMs) {
  return (
    BIN_LADDER.find((step) => Math.ceil(spanMs / (step * 60_000)) <= 50) ??
    BIN_LADDER[BIN_LADDER.length - 1]
  )
}

// Round a value up to the nearest 1/2/5 × 10^k so the y-axis top and its
// gridline labels land on round QSO/h numbers.
function niceCeil(v) {
  if (v <= 0) return 1
  const p = 10 ** Math.floor(Math.log10(v))
  return [1, 2, 5, 10].map((m) => m * p).find((n) => n >= v)
}

// Local HH:mm for axis labels. qso_at is stored UTC ISO; only the label is
// localized (operators reason about "the 2pm lull" in local time, same as the
// clock and contact list).
function formatLocalHm(ms) {
  const d = new Date(ms)
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  return `${h}:${m}`
}

// Series colors, indexed i % length. The four theme signal colors are tuned
// per-theme to read against --theme-surface; the color-mix derivatives shift
// toward --theme-text to keep contrast for overflow modes (same trick the
// themes use for their own derived variables). Busiest modes get the most
// distinct colors (modes are ordered count-descending), and real events run
// 2–4 modes, so overflow is largely theoretical.
const SERIES_COLORS = [
  'var(--theme-accent)',
  'var(--theme-success)',
  'var(--theme-warning)',
  'var(--theme-danger)',
  'color-mix(in srgb, var(--theme-accent) 45%, var(--theme-text))',
  'color-mix(in srgb, var(--theme-success) 45%, var(--theme-text))',
  'color-mix(in srgb, var(--theme-warning) 45%, var(--theme-text))',
  'color-mix(in srgb, var(--theme-danger) 45%, var(--theme-text))',
]

export default function RateGraph() {
  const contacts =
    useLiveQuery(() => db.contacts.filter((c) => !c.deleted).toArray(), []) ?? []

  // Measure the plot area so the SVG is drawn in true pixels (no viewBox
  // stretching, which would distort text and stroke widths). A callback ref
  // (not a mount effect) attaches the observer: the plot div can mount on a
  // later render — the empty-state path renders no plot at all — and the
  // callback fires whenever that node actually appears or unmounts.
  const roRef = useRef(null)
  const [size, setSize] = useState({ w: 0, h: 0 })
  const plotRef = useCallback((el) => {
    if (roRef.current) {
      roRef.current.disconnect()
      roRef.current = null
    }
    if (el) {
      roRef.current = new ResizeObserver(([entry]) => {
        const { width, height } = entry.contentRect
        setSize({ w: width, h: height })
      })
      roRef.current.observe(el)
    }
  }, [])

  if (contacts.length === 0) {
    return (
      <div className="rate-graph">
        <h3>QSO Rate (QSO/h)</h3>
        <p className="placeholder">No contacts yet</p>
      </div>
    )
  }

  // Contact times (bad timestamps filtered out so they can't poison min/max,
  // same guard ContactList uses for display).
  const times = contacts
    .map((c) => ({ t: new Date(c.qso_at).getTime(), mode: c.mode || 'Unknown' }))
    .filter((x) => Number.isFinite(x.t))

  // Modes ordered by count descending — same shape as StatsPanel's tally;
  // this fixes each mode's color and stacking order.
  const modeCounts = {}
  times.forEach((x) => {
    modeCounts[x.mode] = (modeCounts[x.mode] || 0) + 1
  })
  const modes = Object.entries(modeCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([m]) => m)

  const first = Math.min(...times.map((x) => x.t))
  const last = Math.max(...times.map((x) => x.t))
  const binMin = pickBinMinutes(last - first)
  const binMs = binMin * 60_000
  // Clock-aligned edges (:00/:15/:30…) via epoch flooring; binCount is always
  // ≥ 1, so a single contact / zero span yields exactly one bar.
  const start = Math.floor(first / binMs) * binMs
  const binCount = Math.floor((last - start) / binMs) + 1

  // bins[i][mode] = count; then rate() normalizes any bin width to QSO/h.
  const bins = Array.from({ length: binCount }, () => ({}))
  times.forEach((x) => {
    const i = Math.floor((x.t - start) / binMs)
    bins[i][x.mode] = (bins[i][x.mode] || 0) + 1
  })
  const rate = (count) => count * (60 / binMin)
  const yMax = niceCeil(
    Math.max(
      1,
      ...bins.map((b) => rate(Object.values(b).reduce((s, n) => s + n, 0))),
    ),
  )

  const { w, h } = size
  const M = { top: 6, right: 6, bottom: 18, left: 34 }
  const plotW = w - M.left - M.right
  const plotH = h - M.top - M.bottom
  const ready = w > 0 && h > 0 && plotW > 0 && plotH > 0

  const barStep = ready ? plotW / binCount : 0
  const barW = Math.max(barStep - 1, 1)
  const yFor = (r) => M.top + plotH - (r * plotH) / yMax
  // One x-label roughly every 60px.
  const labelEvery = ready ? Math.max(1, Math.ceil(binCount / (plotW / 60))) : 1

  return (
    <div className="rate-graph">
      <h3>QSO Rate (QSO/h)</h3>
      <div className="rate-graph-plot" ref={plotRef}>
        {ready && (
          <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
            {/* y gridlines + labels at ¼, ½, ¾, 1 × yMax */}
            {[0.25, 0.5, 0.75, 1].map((f) => {
              const y = yFor(f * yMax)
              return (
                <g key={f}>
                  <line
                    x1={M.left}
                    y1={y}
                    x2={w - M.right}
                    y2={y}
                    stroke="var(--theme-border-subtle)"
                  />
                  <text
                    x={M.left - 4}
                    y={y + 3}
                    textAnchor="end"
                    fontSize="10"
                    fill="var(--theme-text-faint)"
                  >
                    {Math.round(f * yMax)}
                  </text>
                </g>
              )
            })}
            {/* baseline + 0 label */}
            <line
              x1={M.left}
              y1={M.top + plotH}
              x2={w - M.right}
              y2={M.top + plotH}
              stroke="var(--theme-border)"
            />
            <text
              x={M.left - 4}
              y={M.top + plotH + 3}
              textAnchor="end"
              fontSize="10"
              fill="var(--theme-text-faint)"
            >
              0
            </text>
            {/* stacked bars */}
            {bins.map((b, i) => {
              let cum = 0
              const x = M.left + i * barStep
              return modes.map((mode, mi) => {
                const r = rate(b[mode] || 0)
                if (r <= 0) return null
                const y = yFor(cum + r)
                cum += r
                return (
                  <rect
                    key={`${i}-${mode}`}
                    x={x}
                    y={y}
                    width={barW}
                    height={(r * plotH) / yMax}
                    fill={SERIES_COLORS[mi % SERIES_COLORS.length]}
                    stroke="var(--theme-surface)"
                    strokeWidth="1"
                  />
                )
              })
            })}
            {/* x-axis ticks + local HH:mm labels */}
            {bins.map((_, i) => {
              if (i % labelEvery !== 0) return null
              const x = M.left + i * barStep + barW / 2
              return (
                <g key={`x-${i}`}>
                  <line
                    x1={x}
                    y1={M.top + plotH}
                    x2={x}
                    y2={M.top + plotH + 3}
                    stroke="var(--theme-border)"
                  />
                  <text
                    x={x}
                    y={M.top + plotH + 13}
                    textAnchor="middle"
                    fontSize="10"
                    fill="var(--theme-text-faint)"
                  >
                    {formatLocalHm(start + i * binMs)}
                  </text>
                </g>
              )
            })}
          </svg>
        )}
      </div>
      <div className="rate-graph-legend">
        {modes.map((mode, i) => (
          <span key={mode}>
            <span
              className="swatch"
              style={{ background: SERIES_COLORS[i % SERIES_COLORS.length] }}
            />
            {mode}
          </span>
        ))}
      </div>
    </div>
  )
}

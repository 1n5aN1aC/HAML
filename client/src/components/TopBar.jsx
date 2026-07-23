// Slim bar always shown at the top: brand, tab navigation, theme picker, the
// active event, and whether the realtime (WebSocket) signal layer is reachable.
import { useLayoutEffect, useRef, useState } from 'react'
import { THEMES } from '../themes.js'

const TABS = [
  { id: 'logging', label: 'Logging' },
  { id: 'stats', label: 'Stats' },
  { id: 'settings', label: 'Settings' },
  { id: 'admin', label: 'Admin' },
]

// Does everything still fit on one line? Drives .stacked (see .top-bar in app.css)
// Measured rather than expressed as a media query: the event name is per-event:
// no fixed breakpoint is right for both "FD 2026" and "SARC Summer POTA — US-1234".
//
// Every width summed here is intrinsic (shrink-to-fit), so it reads the same whether stacked or not.
// This stops oscillating: stacking must never change the number that decides to stack.
// The one element that would break that rule is .tabs, which gets flex-basis: 100% when stacked
// and so reports the full bar width — hence summing the tab buttons inside it rather than the nav itself.
function fitsOneRow(bar) {
  const gapOf = (el) => parseFloat(getComputedStyle(el).columnGap) || 0
  const style = getComputedStyle(bar)
  const kids = [...bar.children]

  const need = kids.reduce((sum, el) => {
    // the spacer is flex:1, so it has no intrinsic width to measure — charge it
    // minimum we want it to hold open
    if (el.classList.contains('spacer')) return sum
    if (el.classList.contains('tabs')) {
      const tabs = [...el.children]
      return sum + tabs.reduce((w, t) => w + t.offsetWidth, 0) + gapOf(el) * Math.max(0, tabs.length - 1)
    }
    return sum + el.offsetWidth
  }, parseFloat(style.paddingLeft) + parseFloat(style.paddingRight) + gapOf(bar) * Math.max(0, kids.length - 1))

  // clientWidth includes padding, which `need` accounts for above
  return need <= bar.clientWidth
}

export default function TopBar({ eventName, connected, activeTab, onTab, theme, onTheme }) {
  const barRef = useRef(null)
  const [stacked, setStacked] = useState(false)

  // layout effect so the measurement lands before paint — no flash of the
  // one-row layout on a narrow screen
  useLayoutEffect(() => {
    const bar = barRef.current
    if (!bar) return
    const measure = () => setStacked(!fitsOneRow(bar))
    measure()
    // Bar itself catches viewport resizes; children catch their own content changing width.
    // (a late-loading font, a new event name). Toggling .stacked resizes .tabs and re-fires this,
    // Measurement ignores the stacked state, so it recomputes the same answer and React drops the
    // no-op update — it settles in one pass.
    const ro = new ResizeObserver(measure)
    ro.observe(bar)
    for (const el of bar.children) ro.observe(el)
    return () => ro.disconnect()
  }, [eventName])

  return (
    <header ref={barRef} className={stacked ? 'top-bar stacked' : 'top-bar'}>
      <div className="brand">
        <img
          className="brand-icon"
          src={connected ? '/favicon.svg' : '/favicon-disconnected.svg'}
          alt=""
        />
        <span className="brand-text">HAML</span>
      </div>
      <nav className="tabs">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            className={id === activeTab ? 'tab active' : 'tab'}
            onClick={() => onTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>
      <span className="spacer" />
      <span className="event-name">{eventName}</span>
      <label
        className="theme-picker"
        title={`Theme: ${THEMES.find((t) => t.id === theme)?.label ?? theme}`}
      >
        <span className="theme-picker-icon" aria-hidden="true">
          🎨
        </span>
        <select value={theme} aria-label="Theme" onChange={(e) => onTheme(e.target.value)}>
          {THEMES.map(({ id, label }) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>
    </header>
  )
}

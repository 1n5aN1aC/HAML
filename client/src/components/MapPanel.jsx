// The ARRL section map (bottom of the right pane). Rendered via <object>
// (not <img>) so the SVG's section shapes stay scriptable via contentDocument
// — hover tooltips now, worked-section coloring later.
//
// Self-contained: watches Dexie for worked sections itself, so it can be
// dropped anywhere with just <MapPanel />. The counter/popup overlay is sized
// in em, so a usage site can scale it by setting font-size on .map-panel.

import { useEffect, useRef, useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { db } from '../db.js'
import { SECTION_NAMES, TRACKED_SECTIONS } from '../sections.js'

const WORKED_FILL = '#0ea5e9'
const UNWORKED_FILL = '#ffffff'

export default function MapPanel() {
  const objectRef = useRef(null)
  const tooltipRef = useRef(null)
  const overlayRef = useRef(null)
  const [showMissing, setShowMissing] = useState(false)
  const [svgReady, setSvgReady] = useState(false)

  // Worked sections, live from the contact log (templates that track sections
  // name the field "section"; contacts without one simply don't count).
  const worked =
    useLiveQuery(async () => {
      const sections = new Set()
      await db.contacts
        .filter((c) => !c.deleted)
        .each((c) => {
          const s = c.fields?.section?.toUpperCase()
          if (s) sections.add(s)
        })
      return sections
    }, []) ?? new Set()

  const missing = TRACKED_SECTIONS.filter((s) => !worked.has(s))
  const workedCount = TRACKED_SECTIONS.length - missing.length

  // Color the map: each tracked section's shape (id = abbreviation) filled by
  // whether it's been worked. Runs on every log change, and again once the
  // SVG finishes loading (svgReady) in case contacts arrived first.
  useEffect(() => {
    const svgDoc = objectRef.current?.contentDocument
    if (!svgReady || !svgDoc) return
    TRACKED_SECTIONS.forEach((s) => {
      const el = svgDoc.getElementById(s)
      if (el) el.style.fill = worked.has(s) ? WORKED_FILL : UNWORKED_FILL
    })
  }, [svgReady, worked])

  // Close the popup on any pointer-down outside the overlay. (Clicks landing
  // inside the SVG's own document don't bubble out to ours, so clicking the
  // map itself won't close it — same as the old project.)
  useEffect(() => {
    if (!showMissing) return
    const close = (e) => {
      if (!overlayRef.current?.contains(e.target)) setShowMissing(false)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [showMissing])

  useEffect(() => {
    const obj = objectRef.current
    let attached = false

    // One delegated listener on the SVG document: whichever shape the cursor
    // is over, its nearest id'd ancestor names the section. Mouse coordinates
    // inside the <object> are relative to its viewport, so offset by the
    // object's position to place the (position:fixed) tooltip on the page.
    const setup = () => {
      const svgDoc = obj.contentDocument
      if (attached || !svgDoc?.documentElement) return
      attached = true
      setSvgReady(true)  // lets the coloring effect (re-)apply fills
      const tip = tooltipRef.current

      svgDoc.addEventListener('mousemove', (e) => {
        const id = e.target.closest?.('[id]')?.id
        const name = SECTION_NAMES[id]
        if (!name) {
          tip.style.display = 'none'
          return
        }
        tip.textContent = `${id} - ${name}`
        tip.style.display = 'block'
        const rect = obj.getBoundingClientRect()
        tip.style.left = `${rect.left + e.clientX - tip.offsetWidth - 12}px`
        tip.style.top = `${rect.top + e.clientY + 12}px`
      })
      svgDoc.addEventListener('mouseleave', () => {
        tooltipRef.current.style.display = 'none'
      })
    }

    // The load event covers the normal case; the readyState check covers the
    // SVG already being loaded (e.g. from cache) before this effect runs.
    if (obj.contentDocument?.readyState === 'complete') setup()
    obj.addEventListener('load', setup)
    return () => obj.removeEventListener('load', setup)
  }, [])

  return (
    <div className="map-panel">
      <object
        ref={objectRef}
        type="image/svg+xml"
        data="/map.svg"
        aria-label="ARRL Section Map"
      />
      <div ref={tooltipRef} className="map-tooltip" />
      <div ref={overlayRef}>
        {showMissing && (
          <div className="map-missing">
            <div className="map-missing-header">
              <span>Missing Sections</span>
              <button title="Close" onClick={() => setShowMissing(false)}>
                &times;
              </button>
            </div>
            <div className="map-missing-list">
              {missing.length === 0 ? (
                <em>All sections worked! 🎉</em>
              ) : (
                missing.map((s) => (
                  <span key={s} className="section-chip" title={SECTION_NAMES[s]}>
                    {s}
                  </span>
                ))
              )}
            </div>
          </div>
        )}
        <button
          className="map-counter"
          title="Show missing sections"
          onClick={() => setShowMissing((v) => !v)}
        >
          {workedCount} / {TRACKED_SECTIONS.length}
        </button>
      </div>
    </div>
  )
}

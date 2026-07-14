// The bottom-right pane: toggles between the section map and contact stats.
// The toggle button overlays the pane's bottom-right corner (the counter owns
// bottom-left). Last choice persists across restarts; first run shows stats.

import { useState } from 'react'
import MapPanel from '../MapPanel.jsx'
import StatsPanel from './StatsPanel.jsx'

const VIEW_KEY = 'haml-map-stats-view'

export default function MapStatsPanel() {
  const [view, setView] = useState(() =>
    localStorage.getItem(VIEW_KEY) === 'map' ? 'map' : 'stats',
  )

  const toggle = () => {
    const next = view === 'map' ? 'stats' : 'map'
    localStorage.setItem(VIEW_KEY, next)
    setView(next)
  }

  return (
    <div className="map-stats-panel">
      {view === 'map' ? <MapPanel /> : <StatsPanel />}
      <button
        className="panel-toggle-btn"
        title={view === 'map' ? 'Show Detailed Stats' : 'Back to Map'}
        onClick={toggle}
      >
        {view === 'map' ? 'Stats 📊' : 'Map 🗺️'}
      </button>
    </div>
  )
}

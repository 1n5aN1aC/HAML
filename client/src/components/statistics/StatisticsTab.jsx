// Statistics tab: contact stats fill the left half; the right half is split
// into two panels — a future display on top, the section map below.
import StatisticsPanel from './StatisticsPanel.jsx'
import MapPanel from '../MapPanel.jsx'

export default function StatisticsTab() {
  return (
    <main className="panes stats-page">
      <section className="stats-left">
        <StatisticsPanel />
      </section>
      <aside className="stats-right">
        <div className="stats-top">
          <p className="placeholder">Coming soon</p>
        </div>
        <div className="stats-bottom">
          <MapPanel />
        </div>
      </aside>
    </main>
  )
}

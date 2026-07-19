// Stats tab: contact stats fill the left half; the right half is split
// into two panels — the QSO rate graph on top, the section map below.
import StatsPanel from './StatsPanel.jsx'
import RateGraph from './RateGraph.jsx'
import MapPanel from '../MapPanel.jsx'

export default function StatsTab() {
  return (
    <main className="panes stats-page">
      <section className="stats-left">
        <StatsPanel />
      </section>
      <aside className="stats-right">
        <div className="stats-top">
          <RateGraph />
        </div>
        <div className="stats-bottom">
          <MapPanel />
        </div>
      </aside>
    </main>
  )
}

// Settings tab: client-side settings plus the ADIF importer. Replaces the
// old TopBar gear modal and the invisible `import` tab.
import { useState } from 'react'
import ExportSection from './ExportSection.jsx'
import ImportSection from './ImportSection.jsx'

export default function SettingsTab({ config, event, session, clientUuid }) {
  // Bumping the key re-mounts the importer, clearing the loaded file and
  // every mapping — cheaper than threading a reset through its state.
  const [importKey, setImportKey] = useState(0)

  return (
    <div className="tab-page settings-page">
      <section className="settings-section">
        <h2>Client settings</h2>
        <p className="settings-soon">Sounds on/off coming soon.</p>
        <p className="settings-soon">Miles/km switch coming soon.</p>
        <p className="settings-soon">More settings coming soon.</p>
      </section>
      <ExportSection event={event} />
      <ImportSection
        key={importKey}
        config={config}
        session={session}
        clientUuid={clientUuid}
        onReset={() => setImportKey((k) => k + 1)}
      />
    </div>
  )
}

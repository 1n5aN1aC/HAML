// Settings modal, opened from the TopBar gear. Holds future client settings;
// for now it only launches the ADIF importer (the invisible `import` tab).
import { useEffect } from 'react'

export default function SettingsModal({ onImportAdif, onClose }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-header">
          <span>Settings</span>
          <button type="button" className="modal-close" onClick={onClose} title="Close">
            ✕
          </button>
        </div>
        <p className="settings-soon">Sounds on/off coming soon.</p>
        <p className="settings-soon">Miles/km switch coming soon.</p>
        <p className="settings-soon">More settings coming soon.</p>
        <div className="modal-actions">
          <button type="button" className="btn-primary" onClick={onImportAdif}>
            Import ADIF as contacts
          </button>
          <span className="spacer" />
          <button type="button" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}

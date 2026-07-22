// The "Export Event for Submission" dialog. Unlike the Full ADIF modal it
// resolves nothing: the Template's export block already decided the format,
// the fields and the band/mode mapping (docs/CLIENT.md). What is left is what
// only the operator knows — the prompt answers, such as which park was
// activated — plus the filename and an honest account of what the file will
// and will not contain.
//
// Nothing here validates the Template. A required prompt left blank is the one
// thing that blocks export, because a filename cannot be built without it;
// everything else is a warning the operator may override, matching how dupe
// warnings behave in the entry form.
import { useState } from 'react'
import { download } from '../../db.js'
import { FORMATS, exportPrompts, unmappedValues } from '../../submission-export.js'
import FieldInput from '../logging/FieldInput.jsx'

export default function SubmissionModal({ event, config, contacts, pending, onClose }) {
  const format = FORMATS[config.format]
  const prompts = exportPrompts(config)
  const [answers, setAnswers] = useState(() =>
    Object.fromEntries(prompts.map((p) => [p.name, p.default ?? ''])),
  )
  // The filename is computed until the operator edits it, then it is theirs —
  // the same rule the template editor uses for an auto-slugged id.
  const [typedName, setTypedName] = useState('')
  const [nameTouched, setNameTouched] = useState(false)

  const ctx = { event, config, prompts: answers }
  const computedName = format.filename(contacts, ctx)
  const filename = nameTouched ? typedName : computedName

  const missing = prompts.filter((p) => p.required && !answers[p.name]?.trim())
  const unmapped = unmappedValues(contacts, config)
  const ready = missing.length === 0 && filename.trim()

  function runExport() {
    download(filename.trim(), format.build(contacts, ctx))
    onClose()
  }

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="modal export-modal">
        <div className="modal-header">
          <span>Export for {format.label} submission</span>
          <button type="button" className="modal-close" onClick={onClose} title="Close">
            ✕
          </button>
        </div>

        <p>
          <strong>{contacts.length}</strong> contact{contacts.length === 1 ? '' : 's'}
          {pending > 0 && <> ({pending} not yet synced to the server)</>}
        </p>

        {prompts.length > 0 && (
          <div className="import-section">
            <h2>
              Your details{' '}
              <span className="import-note">
                what this log doesn’t record, but {format.label} needs
              </span>
            </h2>
            <div className="entry-fields">
              {prompts.map((p) => (
                <label key={p.name}>
                  {p.label}:{p.required && '*'}
                  <FieldInput
                    field={p}
                    value={answers[p.name] ?? ''}
                    onChange={(v) => setAnswers({ ...answers, [p.name]: v })}
                  />
                </label>
              ))}
            </div>
          </div>
        )}

        <div className="import-section">
          <h2>
            Filename <span className="import-note">edit if this activation needs a different name</span>
          </h2>
          <input
            className="export-filename"
            value={filename}
            onChange={(e) => {
              setNameTouched(true)
              setTypedName(e.target.value)
            }}
          />
        </div>

        {unmapped.length > 0 && (
          <p className="import-hint export-warning">
            ⚠ This template has no mapping for{' '}
            {unmapped.map((u, i) => (
              <span key={`${u.key}-${u.value}`}>
                {i > 0 && ', '}
                {u.key} “<strong>{u.value}</strong>” ({u.count})
              </span>
            ))}
            . Those contacts export with that tag missing, which the sponsor will
            likely reject. Correct the contacts, or add the value to the template’s
            mapping in Admin → Templates.
          </p>
        )}

        {missing.length > 0 && (
          <p className="import-hint">
            Fill in {missing.map((p) => p.label).join(', ')} to export.
          </p>
        )}

        <div className="modal-actions">
          <button type="button" onClick={onClose}>Cancel</button>
          <span className="spacer" />
          <button type="button" className="btn-primary" disabled={!ready} onClick={runExport}>
            Export {contacts.length} contact{contacts.length === 1 ? '' : 's'}
          </button>
        </div>
      </div>
    </div>
  )
}
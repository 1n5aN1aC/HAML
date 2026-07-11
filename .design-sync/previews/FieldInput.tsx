// FieldInput is a bare <input>/<select> for one template field. Compose it in
// the labeled wrapper the entry form uses so each variant reads as real usage.
// Validation colors latch statically: a value matching the field pattern shows
// the green (v-ok) border; a non-matching value (unfocused) shows red (v-bad).
import { FieldInput } from 'haml-client'

const noop = () => {}

const classField = {
  name: 'class', label: 'Class', type: 'text', required: true, order: 1,
  validation: { pattern: '\\d{1,2}[A-F]', message: 'Class must be a Field Day class like 3A' },
}
const choiceField = { name: 'ant', label: 'Antenna', type: 'choice', options: ['Dipole', 'Vertical', 'Beam'] }
const numberField = { name: 'power', label: 'Power (W)', type: 'number' }

const Row = ({ label, children }: { label: string; children: any }) => (
  <label style={{ display: 'flex', flexDirection: 'column', gap: 4, maxWidth: 220, fontSize: 13, fontFamily: 'system-ui, sans-serif' }}>
    {label}
    {children}
  </label>
)

export const TextValid = () => (
  <Row label="Class (valid)"><FieldInput field={classField} value="3A" onChange={noop} /></Row>
)

export const TextInvalid = () => (
  <Row label="Class (invalid)"><FieldInput field={classField} value="XX" onChange={noop} /></Row>
)

export const Choice = () => (
  <Row label="Antenna"><FieldInput field={choiceField} value="Vertical" onChange={noop} /></Row>
)

export const Number = () => (
  <Row label="Power (W)"><FieldInput field={numberField} value="100" onChange={noop} /></Row>
)

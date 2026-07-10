// One template-defined field input (ADR-0003 types: text, number, choice).
// Shared by the entry form and the edit modal so fields render identically.
export default function FieldInput({ field, value, onChange }) {
  if (field.type === 'choice') {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">—</option>
        {(field.options ?? []).map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
    )
  }
  return (
    <input
      type={field.type === 'number' ? 'number' : 'text'}
      value={value}
      onChange={(e) =>
        onChange(field.type === 'number' ? e.target.value : e.target.value.toUpperCase())
      }
    />
  )
}

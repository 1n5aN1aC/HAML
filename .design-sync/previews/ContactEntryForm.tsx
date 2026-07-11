import { ContactEntryForm } from 'haml-client'
import { config, session, clientUuid } from './_fixtures'

const noop = () => {}

export const Ready = () => (
  <div style={{ maxWidth: 660 }}>
    <ContactEntryForm config={config} session={session} clientUuid={clientUuid} disabled={false} />
  </div>
)

export const Gated = () => (
  <div style={{ maxWidth: 660 }}>
    <ContactEntryForm config={config} session={session} clientUuid={clientUuid} disabled={true} />
  </div>
)

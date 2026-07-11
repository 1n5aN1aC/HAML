import { StatusBar } from 'haml-client'
import { config, session, conflicts } from './_fixtures'

const noop = () => {}

export const Default = () => (
  <div style={{ maxWidth: 920 }}>
    <StatusBar session={session} onSession={noop} config={config} />
  </div>
)

export const BandConflict = () => (
  <div style={{ maxWidth: 920 }}>
    <StatusBar session={session} onSession={noop} config={config} conflicts={conflicts} />
  </div>
)

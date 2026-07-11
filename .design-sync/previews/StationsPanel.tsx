import { StationsPanel } from 'haml-client'
import { stations, clientUuid, config } from './_fixtures'

export const Roster = () => (
  <div style={{ maxWidth: 340 }}>
    <StationsPanel
      stations={stations}
      clientUuid={clientUuid}
      conflictUuids={new Set(['client-other-2'])}
      bands={config.bands}
    />
  </div>
)

export const Empty = () => (
  <div style={{ maxWidth: 340 }}>
    <StationsPanel stations={[]} clientUuid={clientUuid} conflictUuids={new Set()} bands={config.bands} />
  </div>
)

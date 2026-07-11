import { ChatPanel } from 'haml-client'
import { chat } from './_fixtures'

const noop = () => {}

export const Active = () => (
  <div style={{ height: 360, maxWidth: 420, display: 'flex' }}>
    <ChatPanel messages={chat} onSend={noop} onResend={noop} disabled={false} />
  </div>
)

export const Empty = () => (
  <div style={{ height: 360, maxWidth: 420, display: 'flex' }}>
    <ChatPanel messages={[]} onSend={noop} onResend={noop} disabled={false} />
  </div>
)

export const Disabled = () => (
  <div style={{ height: 360, maxWidth: 420, display: 'flex' }}>
    <ChatPanel messages={chat} onSend={noop} onResend={noop} disabled={true} />
  </div>
)

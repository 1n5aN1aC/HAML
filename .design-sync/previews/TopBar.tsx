import { TopBar } from 'haml-client'

const noop = () => {}

export const Connected = () => (
  <TopBar eventName="ARRL Field Day" connected={true} activeTab="logging" onTab={noop} theme="light" onTheme={noop} />
)

export const Offline = () => (
  <TopBar eventName="ARRL Field Day" connected={false} activeTab="radio" onTab={noop} theme="dark" onTheme={noop} />
)

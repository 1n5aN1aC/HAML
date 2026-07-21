// WebSocket signal layer (docs/ARCHITECTURE.md): presence heartbeats out; roster, chat
// broadcasts, pokes, and event notices in. Pure optimization — when the
// socket is down, sync polling continues, presence goes stale, chat pauses.
const HEARTBEAT_INTERVAL = 5_000
const RECONNECT_BASE = 1_000
const RECONNECT_CAP = 30_000

let mgr = null

// handlers: { onConnect, onDisconnect, onPresence, onChat, onPoke, onEvent,
//             onChatCleared }
// Returns a stop function.
export function startSocket(handlers) {
  stopSocket()
  const m = {
    handlers,
    ws: null,
    stopped: false,
    presence: null,
    heartbeatTimer: null,
    reconnectTimer: null,
    delay: RECONNECT_BASE,
  }
  mgr = m
  connect(m)
  return () => {
    if (mgr === m) stopSocket()
  }
}

export function stopSocket() {
  if (!mgr) return
  mgr.stopped = true
  clearInterval(mgr.heartbeatTimer)
  clearTimeout(mgr.reconnectTimer)
  if (mgr.ws) mgr.ws.close()
  mgr = null
}

// Update what the heartbeat announces; also sends immediately (a band/mode
// change must show up on other screens right away, not on the next tick).
export function setPresence(presence) {
  if (!mgr) return
  mgr.presence = presence
  sendPresence(mgr)
}

// Send a chat message. Returns false when the socket is down — the caller
// marks the message failed for manual resend (see docs/CLIENT.md).
export function sendChat(message) {
  if (!mgr || !isOpen(mgr.ws)) return false
  mgr.ws.send(JSON.stringify({ type: 'chat', ...message }))
  return true
}

const isOpen = (ws) => ws && ws.readyState === WebSocket.OPEN

function sendPresence(m) {
  if (m.presence && isOpen(m.ws)) {
    m.ws.send(JSON.stringify({ type: 'presence', ...m.presence }))
  }
}

function connect(m) {
  if (m.stopped) return
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${window.location.host}/ws`)
  m.ws = ws

  ws.onopen = () => {
    m.delay = RECONNECT_BASE
    sendPresence(m)
    m.heartbeatTimer = setInterval(() => sendPresence(m), HEARTBEAT_INTERVAL)
    m.handlers.onConnect?.()
  }

  ws.onmessage = (event) => {
    let data
    try {
      data = JSON.parse(event.data)
    } catch {
      return
    }
    if (data.type === 'presence_list') m.handlers.onPresence?.(data.stations)
    else if (data.type === 'chat') m.handlers.onChat?.(data.message)
    else if (data.type === 'poke') m.handlers.onPoke?.()
    else if (data.type === 'event') m.handlers.onEvent?.(data.event_uuid)
    else if (data.type === 'chat_cleared') m.handlers.onChatCleared?.()
  }

  ws.onclose = () => {
    clearInterval(m.heartbeatTimer)
    m.ws = null
    if (m.stopped) return
    m.handlers.onDisconnect?.()
    m.reconnectTimer = setTimeout(() => connect(m), m.delay)
    m.delay = Math.min(m.delay * 2, RECONNECT_CAP)
  }

  ws.onerror = () => ws.close()
}

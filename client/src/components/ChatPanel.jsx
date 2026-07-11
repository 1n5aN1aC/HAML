// Chat panel (plan §3.5): scrolling history + input. Sending requires operator
// identity (callsign + initials) since every message is stamped with it.
// Failed sends (socket down / lost in a blip) show a manual resend button.
import { useEffect, useRef, useState } from 'react'

function formatTime(iso) {
  const d = new Date(iso)
  return isNaN(d) ? '' : d.toISOString().slice(11, 16)
}

export default function ChatPanel({ messages, onSend, onResend, disabled }) {
  const [text, setText] = useState('')
  const scrollRef = useRef(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  function submit(e) {
    e.preventDefault()
    const trimmed = text.trim()
    if (!trimmed) return
    onSend(trimmed)
    setText('')
  }

  return (
    <div className="chat-panel">
      <h2>Chat</h2>
      <div className="chat-messages" ref={scrollRef}>
        {messages.length === 0 && <p className="placeholder">No messages yet</p>}
        {messages.map((m) => (
          <div key={m.uuid} className={`chat-msg${m.status === 'failed' ? ' failed' : ''}`}>
            <span className="chat-time">{formatTime(m.sent_at)}</span>{' '}
            <span className="chat-from">{m.operator_callsign}:</span> {m.text}
            {m.status === 'pending' && <span className="chat-flag" title="Sending…"> …</span>}
            {m.status === 'failed' && (
              <button className="chat-resend" onClick={() => onResend(m.uuid)}>
                resend
              </button>
            )}
          </div>
        ))}
      </div>
      <form className="chat-form" onSubmit={submit}>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={144}
          placeholder={disabled ? 'Set callsign & initials to chat' : 'Type a chat message…'}
          disabled={disabled}
        />
      </form>
    </div>
  )
}

// Tiny sound effects: one reused Audio element per sound, rewound on each
// play. Autoplay can be blocked until the user first interacts with the
// page, so failures are swallowed.
import chatUrl from './sounds/chat.mp3'
import submitUrl from './sounds/submit.mp3'
import duplicateUrl from './sounds/Duplicate.wav'
import dxUrl from './sounds/dx.wav'

function player(url) {
  const audio = new Audio(url)
  return () => {
    audio.currentTime = 0
    audio.play().catch(() => {})
  }
}

export const playChat = player(chatUrl)
export const playSubmit = player(submitUrl)
export const playDuplicate = player(duplicateUrl)
export const playDx = player(dxUrl)

// Rejected beep: synthesized (800 Hz sine, 0.2s decay) instead of a file
// The AudioContext is created lazily and reused;
// construction can fail before the first user gesture, so failures are swallowed like the players above.
let errorCtx = null
export function playError() {
  try {
    errorCtx ??= new AudioContext()
    const osc = errorCtx.createOscillator()
    const gain = errorCtx.createGain()
    osc.connect(gain)
    gain.connect(errorCtx.destination)
    osc.frequency.value = 800
    osc.type = 'sine'
    gain.gain.setValueAtTime(0.3, errorCtx.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.01, errorCtx.currentTime + 0.2)
    osc.start(errorCtx.currentTime)
    osc.stop(errorCtx.currentTime + 0.2)
  } catch {
    /* audio unavailable — stay silent */
  }
}

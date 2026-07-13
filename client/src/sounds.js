// Tiny sound effects: one reused Audio element per sound, rewound on each
// play. Autoplay can be blocked until the user first interacts with the
// page, so failures are swallowed.
import chatUrl from './sounds/chat.mp3'
import submitUrl from './sounds/submit.mp3'

function player(url) {
  const audio = new Audio(url)
  return () => {
    audio.currentTime = 0
    audio.play().catch(() => {})
  }
}

export const playChat = player(chatUrl)
export const playSubmit = player(submitUrl)

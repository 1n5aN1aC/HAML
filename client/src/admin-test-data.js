// Fake-contact generator behind the admin "Add test contacts" button.
// Field values are random strings that satisfy each field's validation
// pattern, so the generated rows pass the same checks as real traffic.
import { newUuid } from './uuid.js'

const UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
const DIGITS = '0123456789'
const WORD = UPPER + UPPER.toLowerCase() + DIGITS + '_'

const pick = (chars) => chars[Math.floor(Math.random() * chars.length)]
const randInt = (lo, hi) => lo + Math.floor(Math.random() * (hi - lo + 1))
const randomChars = (chars, n) =>
  Array.from({ length: n }, () => pick(chars)).join('')

// Build one random string matching `pattern`, covering the regex subset
// templates realistically use: literals, \d \w \s, [...] classes with
// ranges, (…) groups, | alternation, and ? * + {n,m} quantifiers.
// Throws on anything fancier; the caller falls back to brute force.
function sampleOnce(pattern) {
  let i = 0
  const err = () => new Error(`unsupported regex at ${i}: ${pattern}`)

  // note: every alternative is parsed (advancing i), then one is kept, so
  // whether a pattern parses at all doesn't depend on the random picks
  function alternation(inGroup) {
    const alts = [sequence(inGroup)]
    while (pattern[i] === '|') {
      i++
      alts.push(sequence(inGroup))
    }
    return pick(alts)
  }

  function sequence(inGroup) {
    let out = ''
    while (
      i < pattern.length &&
      pattern[i] !== '|' &&
      !(inGroup && pattern[i] === ')')
    ) {
      out += quantified()
    }
    return out
  }

  function quantified() {
    const start = i
    const s = atom()
    const atomSrc = pattern.slice(start, i)
    let lo = 1
    let hi = 1
    const c = pattern[i]
    if (c === '?') {
      i++; lo = 0; hi = 1
    } else if (c === '*') {
      i++; lo = 0; hi = 2
    } else if (c === '+') {
      i++; lo = 1; hi = 3
    } else if (c === '{') {
      const m = /^\{(\d+)(,(\d*))?\}/.exec(pattern.slice(i))
      if (!m) throw err()
      i += m[0].length
      lo = Number(m[1])
      hi = m[2] === undefined ? lo : m[3] ? Number(m[3]) : lo + 2
    }
    // each repetition re-samples the atom so "1234" beats "1111"
    let out = ''
    for (let n = randInt(lo, hi); n > 0; n--) {
      out += n === 1 ? s : sampleOnce(atomSrc)
    }
    return out
  }

  function atom() {
    const c = pattern[i]
    if (c === '(') {
      i++
      if (pattern.startsWith('?:', i)) i += 2
      else if (pattern[i] === '?') throw err() // lookarounds etc.
      const s = alternation(true)
      if (pattern[i] !== ')') throw err()
      i++
      return s
    }
    if (c === '[') return charClass()
    if (c === '\\') {
      i++
      return escapeChar(pattern[i++])
    }
    if (c === '.') {
      i++
      return pick(UPPER + DIGITS)
    }
    if (c === '^' || c === '$') {
      i++
      return ''
    }
    if ('*+?{})'.includes(c)) throw err()
    i++
    return c
  }

  // one character satisfying the escape (a subset pick is still a match)
  function escapeChar(c) {
    if (c === undefined) throw err()
    if (c === 'd') return pick(DIGITS)
    if (c === 'w') return pick(WORD)
    if (c === 's') return ' '
    if (/[A-Za-z0-9]/.test(c)) throw err() // \D, \b, back-refs, …
    return c // escaped punctuation is a literal
  }

  function charClass() {
    i++ // '['
    if (pattern[i] === '^') throw err() // negated classes unsupported
    let chars = ''
    while (pattern[i] !== ']') {
      if (i >= pattern.length) throw err()
      const c = pattern[i]
      if (c === '\\') {
        i++
        chars += escapeChar(pattern[i++])
        continue
      }
      i++
      if (pattern[i] === '-' && pattern[i + 1] !== ']') {
        const hi = pattern[++i]
        if (hi === '\\' || hi === undefined) throw err()
        i++
        for (let k = c.charCodeAt(0); k <= hi.charCodeAt(0); k++)
          chars += String.fromCharCode(k)
      } else {
        chars += c
      }
    }
    i++ // ']'
    if (!chars) throw err()
    return pick(chars)
  }

  const out = alternation(false)
  if (i !== pattern.length) throw err()
  return out
}

// Random string that full-matches `pattern` (same ^(?:…)$ semantics as
// contact-validation.js). The sampler's output is verified against the real
// RegExp; patterns outside its subset get a brute-force try. Returns null
// when nothing converges.
export function matchingString(pattern, maxLength = 12) {
  let re
  try {
    re = new RegExp(`^(?:${pattern})$`)
  } catch {
    return null
  }
  for (let n = 0; n < 20; n++) {
    try {
      const s = sampleOnce(pattern)
      if (re.test(s)) return s
    } catch {
      break // parse failures don't depend on the random picks
    }
  }
  for (let n = 0; n < 500; n++) {
    const s = randomChars(UPPER + DIGITS, randInt(1, Math.min(maxLength, 8)))
    if (re.test(s)) return s
  }
  return null
}

function fieldValue(field) {
  const max = field.max_length ?? 8
  if (field.validation) {
    const s = matchingString(field.validation.pattern, max)
    if (s !== null) return s
  }
  return randomChars(UPPER, randInt(2, Math.min(max, 6)))
}

// Plausible amateur callsign shape: prefix, region digit, suffix.
function randomCallsign() {
  return (
    randomChars(UPPER, randInt(1, 2)) + pick(DIGITS) + randomChars(UPPER, randInt(1, 3))
  )
}

// Build `count` fake contacts for the active event's config, in the exact
// shape POST /api/contacts expects. QSO times are scattered over the last
// two hours so the log looks lived-in; TEST/TT marks the rows as fake.
export function generateTestContacts(config, count = 25) {
  const now = Date.now()
  return Array.from({ length: count }, () => {
    const iso = new Date(now - randInt(0, 7200) * 1000).toISOString()
    return {
      uuid: newUuid(),
      qso_at: iso,
      created_at: iso,
      last_edited: iso,
      remote_callsign: randomCallsign(),
      operator_callsign: 'TEST',
      operator_initials: 'TT',
      client_uuid: 'test-data',
      band: pick(config.bands),
      mode: pick(config.modes),
      deleted: false,
      fields: Object.fromEntries(config.fields.map((f) => [f.name, fieldValue(f)])),
    }
  })
}

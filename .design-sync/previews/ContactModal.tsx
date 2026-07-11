import { ContactModal } from 'haml-client'
import { sampleContact, config, clientUuid } from './_fixtures'

// The modal renders a `position: fixed` backdrop. A wrapper with a `transform`
// becomes the containing block for that fixed layer, so the dialog centers
// inside the card (and the card measures a real height) instead of escaping to
// the browser viewport and clipping.
export const Edit = () => (
  <div style={{ position: 'relative', transform: 'translateZ(0)', minHeight: 560 }}>
    <ContactModal contact={sampleContact} config={config} clientUuid={clientUuid} onClose={() => {}} />
  </div>
)

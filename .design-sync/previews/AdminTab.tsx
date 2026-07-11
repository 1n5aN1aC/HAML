// AdminTab is password-gated (ADR-0004) and its unlocked view calls the server;
// the reachable static state without a backend is the unlock gate.
import { AdminTab } from 'haml-client'

export const Locked = () => (
  <div style={{ maxWidth: 460 }}>
    <AdminTab />
  </div>
)

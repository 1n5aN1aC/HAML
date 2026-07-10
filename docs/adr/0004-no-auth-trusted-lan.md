# No authentication; the LAN is the security boundary

HAML targets a trusted local network at a club event site. There are no user accounts and
no authentication: anyone who can reach the server is a trusted Operator, and anyone may
edit or delete any Contact (net control fixing a typo in someone else's entry is normal).
Accountability comes from stamping, not permissions: every Contact records the Operator
callsign, initials, and Client UUID of its last editor.

The only gate is the Admin page (create/load/backup Events), protected by a simple shared
password configured on the server. This is explicitly *not* a security mechanism — it is
a tripwire to stop people messing around. If HAML is ever exposed to the internet, that
requires a new decision; nothing in this design is safe for that.

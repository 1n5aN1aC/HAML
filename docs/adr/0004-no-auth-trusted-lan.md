# No authentication; the LAN is the security boundary

HAML targets a trusted local network at a club event site. There are no user accounts and
no authentication: anyone who can reach the server is a trusted Operator, and anyone may
edit or delete any Contact (net control fixing a typo in someone else's entry is normal).
Accountability comes from stamping, not permissions: every Contact carries the Operator
callsign and initials it was logged under, plus the Client UUID of the machine that last
edited it (overwritten on every edit). The operator fields carry over through an edit, so
an editor who wants to take ownership of a Contact retypes them in the modal.

The only gate is the Admin page, protected by a simple shared password configured on the
server and sent as a header on every admin request. Behind it: creating an Event from a
Template, activating or deleting a stored Event, backing up the active Event, editing and
deleting the Template files themselves, clearing the Event's chat history, inspecting and
clearing the callsign-lookup cache, and a maintenance action that injects test contacts.
This is explicitly *not* a security mechanism — it is a tripwire to stop people messing
around. If HAML is ever exposed to the internet, that requires a new decision; nothing in
this design is safe for that.

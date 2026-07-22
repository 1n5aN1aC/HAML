BUGS:
- Filed out, but not submitted contact clears on switching tabs

Feature:  Event-specific exports
- WFD Export  (Cabrillo; needs a `choices` key on prompts, and header emission)
- FD Export   (same Cabrillo writer)

POTA Improvements:
- Entry for multiple P2P's
- P2P auto-dash
- P2P should default to "US-"
- Park decoding support

Client:
  Mobile Improvements:
    Fullscreen button
    Submit button
    Mobile Layout
  Offline-First PWA
  Re-submit as Edit with late data

Server:
  Automatic Backup Feature

Location-based improvements:
- Data source: Other countries
    Australia (ACMA) – The Register of Radiocommunications Licences (RRL) offers a full CSV data dump updated daily.
- Data source: HamCall (Buckmaster) ($50)
- Data source: Other online APIs
- Server should live-build Gridsquare, State, ARRL section, ITU zone, CQ zone from location.
- Server should derive unknown location from Grid/Country
- Server should overide a location from state / POTA park / etc.

Supercheck partial:
- FD history
- WFD history
- POTA hunters
- POTA Activators
- LOTW user list  (HB9BZA-maintained list of ~228,000 LoTW-participating calls across 340 DXCC entities)
- Clublog list
- Contest Supercheck Partial

Digital integration shim

Add a README
Properly implement version numbers
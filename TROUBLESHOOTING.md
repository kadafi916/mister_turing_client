# Troubleshooting

## Credential gotchas hit while setting this up

Worth knowing before you re-hit these:

- **RA's Web API key vs. your account password are easy to mix up.**
  retroachievements.org's Settings has both a login password and a
  separate "Web API Key" (Settings → Keys) — `ra_credentials.ini` needs
  the latter. A pasted password gets you a silent-looking `HTTP 401` in
  `mister_status_server.py`'s own log (`/tmp/mister_monitor.log`, grep
  `[RA]`) that surfaces downstream as every game reporting
  `rom_not_recognized` — which looks exactly like a hash-matching problem
  and isn't one. Check the log for `401` before assuming the ROMs are the
  issue.
- **A copy-paste that duplicates the key** (pasting a 32-character key
  twice back-to-back, giving a 64-character value) fails the same way,
  same fix: check the log.
- Both `screenscraper.py` and `retroachievements.py`'s HTTP error paths
  log 401/403 at `warning` (not `debug`) specifically because of the above
  — a credential problem should be loud, not indistinguishable from "no
  data available."

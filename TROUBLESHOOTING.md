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

## RA/artwork silently produce nothing for one specific game

While testing RA/artwork against real games, some loaded fine on-screen
(core/game name showed correctly) but never produced a CRC, hash, or
match of any kind — `/status/rom/details` reported
`"error": "File not found inside ZIP: "` with an empty internal path,
even though the zip was valid and contained exactly the expected file.

Root cause: `mister_status_server.py`'s `is_zip_path()` computes the
in-zip path as whatever text follows `.zip` in the reported ROM path.
Some cores report just the zip's own path with nothing after it (rather
than `zip/file.ext`), so that computation comes out empty — and every
one of `get_zip_file_info_enhanced()`'s five matching strategies is
derived from that empty string, so none can ever match. There was no
fallback for the unambiguous case (a single-entry zip).

This lives in `mister_status_server.py`, not this repo — that file
belongs to the separate AGPL [MiSTer_monitor](https://github.com/chipster6502/MiSTer_monitor)
server project, so the fix was submitted upstream:
[chipster6502/MiSTer_monitor#18](https://github.com/chipster6502/MiSTer_monitor/pull/18).
Until that's merged, the fix is available on
[this branch](https://github.com/kadafi916/MiSTer_monitor/tree/fix/zip-single-entry-no-internal-path)
if you're hitting this now.

If RA/artwork silently produce nothing for a specific game despite
working for others, check `/status/rom/details`'s `error` field for this
exact message before assuming it's a credential or matching problem.

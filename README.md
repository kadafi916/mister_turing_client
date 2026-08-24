# mister_turing_client

A status HUD for MiSTer FPGA that runs on a Turing/XuanFang/UsbMonitor-style
USB smart screen (the ones `turing-smart-screen-python` drives), talking
directly to `mister_status_server.py` — the same hardware-agnostic JSON API
the [MiSTer_monitor](https://github.com/chipster6502/MiSTer_monitor) ESP32
firmware uses. This is a separate integration path, not a port of that
firmware: it runs as a plain Python client instead of on-panel firmware.
See the parent conversation for the full reasoning; short version below.

## Why this exists

MiSTer_monitor's display sketches run as firmware on an ESP32 wired
directly to a bare SPI/parallel TFT panel. The Turing/XuanFang family of
screens is a different kind of device: each has its own onboard MCU
(commonly a WCH CH552T) speaking a fixed USB-serial protocol, reverse
engineered by the `turing-smart-screen-python` project. There's no SPI bus
to wire an ESP32 to and no equivalent "board_hal" swap — so instead of a
firmware port, this is a small Python client, structured the same way
`mister_status_server.py` was already designed to be consumed: poll its
JSON, render, push pixels.

## Why the odd `pylibs/` + `vendor_libs/` layout

This runs **directly on MiSTer's own Buildroot Python 3.9 (armv7l)**, which
has no pip-installable Pillow (no wheel on PyPI for this platform, and no
compiler on-device to build one) and no numpy either. Both are needed:
Pillow for compositing the HUD, numpy because
`turing-smart-screen-python`'s `serialize.image_to_RGB565()` uses it for
the RGB→RGB565 packing.

Both are solved without any on-device compilation or cross-compile
toolchain, by combining two facts:

1. **[piwheels.org](https://www.piwheels.org)** already builds and hosts
   `cp39-cp39-linux_armv7l` wheels for Pillow and numpy (built for
   Raspberry Pi OS), and their glibc symbol floor (`GLIBC_2.4` /
   `GLIBC_2.29`) is comfortably below MiSTer's `glibc 2.31` — so the wheels
   load as-is. Verified with `objdump -T` on the actual `.so` files before
   trusting this, not assumed.
2. Both wheels dynamically link a few shared libraries MiSTer doesn't ship
   (`libjpeg.so.62`, `libopenjp2.so.7`, `libxcb.so.1` + its own chain of
   `libXau`/`libXdmcp`/`libbsd`/`libmd`, and `libopenblas.so.0` for numpy).
   Debian **Bullseye** armhf `.deb` packages (matching MiSTer's own
   `glibc 2.31`) were downloaded from `deb.debian.org`/`security.debian.org`
   and their `.so` files extracted with `ar`/`tar` — no build step, just
   unpacking — into `vendor_libs/`.

`vendor_libs/` is loaded via `LD_LIBRARY_PATH` and `pylibs/` via
`PYTHONPATH`, both set by `mister_turing_client.py` re-executing itself on
startup (see the top of that file) — nothing under MiSTer's own `/usr/lib`
or system Python is touched.

This was verified end-to-end on the real device before writing the client:
`import PIL`, JPEG encode/decode round-trip, TrueType text rendering via
`libfreetype.so.6` (already present on MiSTer, no vendoring needed there),
and the actual `image_to_RGB565` byte-packing via numpy + the vendored
`libopenblas`.

### Reproducing / updating the bundle

```
pylibs/PIL      <- unzip pillow-11.2.1-cp39-cp39-linux_armv7l.whl from piwheels.org, take PIL/
pylibs/numpy    <- unzip numpy-2.0.2-cp39-cp39-linux_armv7l.whl from piwheels.org, take numpy/
pylibs/serial   <- pip install --target=pylibs pyserial   (pure-Python wheel, installs directly)

vendor_libs/    <- .so files extracted from these Debian bullseye armhf .debs:
  libjpeg62-turbo_2.0.6-4_armhf.deb                (deb.debian.org)
  libopenjp2-7_2.4.0-3+deb11u3_armhf.deb           (security.debian.org)
  libxcb1_1.14-3_armhf.deb                         (deb.debian.org)
  libxau6_1.0.9-1_armhf.deb                        (deb.debian.org)
  libxdmcp6_1.1.2-3_armhf.deb                      (deb.debian.org)
  libbsd0_0.11.3-1+deb11u1_armhf.deb               (deb.debian.org)
  libmd0_1.0.3-3_armhf.deb                         (deb.debian.org)
  libopenblas0-pthread_0.3.13+ds-3+deb11u1_armhf.deb (deb.debian.org)
```

Before trusting a newer wheel/package, re-check its glibc floor:
`objdump -T <file>.so | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -V | tail -1`
must be ≤ MiSTer's `ldd --version` (2.31 as of this writing), and its
`NEEDED` entries (`objdump -p <file>.so | grep NEEDED`) must all resolve
against either MiSTer's own `/usr/lib` or `vendor_libs/`.

**Caveat**: this bundle lives under `/media/fat`, which persists across
reboots but is not guaranteed to survive a MiSTer main-binary/OS update —
re-verify (`python3 mister_turing_client.py --once`) after one.

## Layout

```
mister_turing_client.py   entry point - poll + render + push, in a loop
turing_lcd/                trimmed vendored slice of turing-smart-screen-python's
                            library/lcd (LcdCommRevA + base class), GPL-3.0-or-later
fonts/                      RobotoMono (Apache-2.0, from turing-smart-screen-python's res/)
pylibs/                     vendored Pillow, numpy, pyserial (see above)
vendor_libs/                extra .so files Pillow/numpy need beyond MiSTer's own
```

## Usage

```
python3 mister_turing_client.py [--server http://127.0.0.1:8081] [--port AUTO]
                                 [--interval 2.0] [--brightness 80] [--once]
```

Defaults assume `mister_status_server.py` is running on the same MiSTer
(`127.0.0.1:8081`) and the screen is auto-detected by VID/PID/serial
(`USB35INCHIPSV2`, WCH `1a86:5722`). No wrapper script is required —
`LD_LIBRARY_PATH`/`PYTHONPATH` are set by the script re-executing itself.

With no `mister_status_server` reachable, it shows a "Waiting for MiSTer
status server..." screen and keeps polling rather than crashing.

## Features

- **Now Playing** — core/game identity, CPU/memory bars, uptime, SD usage,
  and game/core artwork fetched on demand from ScreenScraper (see below).
- **RetroAchievements** — progress, points and hardcore breakdown, and a
  paginated trophy list, on two auto-rotating pages (no touch input on this
  hardware, so pages cycle on a timer rather than being tapped through).
  An unlock popup overlays whatever page is showing the moment the server
  reports a new one. Entirely server-side — see
  `docs/configuration.md#retroachievements` in MiSTer_monitor for how to
  set up `ra_credentials.ini` on the MiSTer; this client only renders what
  `/status/retroachievements` already reports.

### RetroAchievements: reading `unlocks_tracked` correctly

`ra_status.py`'s `unlocks_tracked` field (rendered on the RA Progress page
as "view-only, pair with odelot/Main_MiSTer" when false) answers a more
specific question than "is the fork installed" — worth knowing so the
message isn't misread:

- **An `RA_`-prefixed core name is not proof the fork is running.** That
  prefix can also come from **MiSTer Companion**, a separate tool that
  renames/organizes core launchers, independent of whether odelot's fork
  is actually installed. Don't infer fork presence from the core name
  alone (a mistake made once already while building this).
- The server's real check (`_unlocks_are_tracked()`) is two things:
  `/media/fat/retroachievements.cfg` existing (the fork's own config), AND
  `/tmp/ra_debug.log`'s mtime being fresh — which only happens with
  **`debug=1`** set in that config. Without `debug=1`, the fork never
  writes its live heartbeat, so the server can't confirm an RA-adapted
  core is running right now even if the fork is genuinely installed and
  working.
- **The fork reads `retroachievements.cfg` at core-load time, not
  continuously.** Editing the file and saving it does nothing to the
  currently-running core — reload/relaunch the game once for a `debug=1`
  change to take effect and `/tmp/ra_debug.log` to start growing.
- `debug=1` is also what MiSTer_monitor's own docs describe as unlocking
  near-instant (<1s) unlock popups instead of the few-seconds
  cloud-polling fallback, and the only route CD systems (PS1/Saturn/Mega
  CD, which the server can't hash locally) have to resolve at all — worth
  turning on regardless of whether the `unlocks_tracked` message matters
  to you.

### Artwork setup (ScreenScraper)

Copy `config.ini.example` to `config.ini` (gitignored - never commit real
credentials) and fill in:

- `ss_user` / `ss_pass` — your regular ScreenScraper member account (free,
  instant signup).
- `ss_dev_user` / `ss_dev_pass` — a ScreenScraper *developer* key, a
  separate credential pair identifying the calling application, not the
  same as your member login. Request one at
  https://www.screenscraper.fr/forumsujets.php?frub=12 (manual, human-reviewed,
  not instant). Without this, artwork stays disabled and everything else
  keeps working — same graceful degradation RetroAchievements uses when
  unconfigured.

`screenscraper.py` and `screenscraper_systems.py` are ports of
MiSTer_monitor's own ScreenScraper integration (same endpoints, same
credential model, same core-name -> systemeid table, same media-type/region
resolution order) - deliberately kept identical since that's proven
behavior against the real API, not a re-guess. One limitation not yet
ported: the firmware's extension-based back-compat disambiguation (e.g. a
2600 cartridge loaded on the 7800 core) - those cores fall back to their
own system, correct in the large majority of cases.

Downloaded art is cached under `artwork_cache/` (gitignored, unbounded SD
card space) keyed by CRC or system id, so nothing is re-fetched once seen.

## Alternatives considered for artwork

ScreenScraper was kept as the sole source rather than adding a fallback:
IGDB (self-service Twitch signup, but no CRC matching and weak arcade/MAME
coverage) and TheGamesDB (same manual-forum-request friction as
ScreenScraper, no better) were considered and set aside - every mainstream
retro frontend (EmulationStation, Batocera, Recalbox) independently landed
on ScreenScraper for the same reason: it's the one built for CRC-based,
arcade-heavy emulation scraping, which is what a MiSTer library actually
needs.

## A `mister_status_server.py` bug this surfaced

While testing RA/artwork against real games, some loaded fine on-screen
(core/game name showed correctly) but never produced a CRC, hash, or
match of any kind — `/status/rom/details` reported
`"error": "File not found inside ZIP: "` with an empty internal path,
even though the zip was valid and contained exactly the expected file.

Root cause: `is_zip_path()` computes the in-zip path as whatever text
follows `.zip` in the reported ROM path. Some cores report just the zip's
own path with nothing after it (rather than `zip/file.ext`), so that
computation comes out empty — and every one of
`get_zip_file_info_enhanced()`'s five matching strategies is derived from
that empty string, so none can ever match. There was no fallback for the
unambiguous case (a single-entry zip). Fixed on the
`fix/zip-single-entry-no-internal-path` branch of the `MiSTer_monitor`
checkout (not this repo — that file belongs to the separate AGPL server
project) and deployed to the test MiSTer; worth upstreaming as a PR.

If RA/artwork silently produce nothing for a specific game despite
working for others, check `/status/rom/details`'s `error` field for this
exact message before assuming it's a credential or matching problem.

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

## Next steps

Touch-equivalent navigation doesn't exist on this hardware (output-only
screen), so further pages would still need to be timer-cycled. Not yet
built: the extension-based system disambiguation mentioned above.
Page rotation speed is configurable via `--page-seconds` (default 12s).

# Technical notes

The engineering backstory behind a few decisions - not needed to use or
install this, just the "why" for anyone curious or extending it.

## Why this exists

MiSTer_monitor is built around the CYD (Cheap Yellow Display) - a good,
cheap choice, but not the cheapest option when you already own something
else. This started from the opposite direction: an AliExpress-bought
Turing/XuanFang-style USB smart screen already sitting around unused,
looking for a reason to exist, rather than a CYD bought specifically to
run MiSTer_monitor.

MiSTer_monitor's display sketches run as firmware on an ESP32 wired
directly to a bare SPI/parallel TFT panel - that's how the CYD works. The
Turing/XuanFang family of screens is a different kind of device entirely:
each has its own onboard MCU (commonly a WCH CH552T) speaking a fixed
USB-serial protocol, reverse engineered by the
[`turing-smart-screen-python`](https://github.com/mathoudebine/turing-smart-screen-python)
project. There's no SPI bus to wire an ESP32 to and no equivalent
"board_hal" swap — so instead of a firmware port, this is a small Python
client, structured the same way `mister_status_server.py` was already
designed to be consumed: poll its JSON, render, push pixels.

## Why the odd pylibs/vendor_libs layout

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
   `glibc 2.31`) are extracted with `ar`/`tar` — no build step, just
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

### What `build_pylibs.sh` actually fetches

```
pylibs/PIL      <- pillow-11.2.1-cp39-cp39-linux_armv7l.whl (piwheels.org), PIL/
pylibs/numpy    <- numpy-2.0.2-cp39-cp39-linux_armv7l.whl (piwheels.org), numpy/
pylibs/serial   <- pyserial-3.5-py2.py3-none-any.whl (PyPI, pure-Python), serial/

vendor_libs/    <- .so files extracted from these Debian bullseye armhf .debs:
  libjpeg62-turbo, libopenjp2-7, libxcb1, libxau6, libxdmcp6,
  libbsd0, libmd0, libopenblas0-pthread
```

`.deb`s are fetched from [snapshot.debian.org](https://snapshot.debian.org)
by content hash rather than `deb.debian.org`/`security.debian.org`'s live
pool, which drops old bullseye point-release versions once superseded -
snapshot.debian.org keeps every version forever, so these exact pinned
files stay fetchable indefinitely.

Before trusting a newer wheel/package (`build_pylibs.sh`'s own trailing
comment has the exact commands), re-check its glibc floor -
`objdump -T <file>.so | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -V | tail -1`
must be ≤ MiSTer's `ldd --version` (2.31 as of this writing) - and its
`NEEDED` entries (`objdump -p <file>.so | grep NEEDED`) must all resolve
against either MiSTer's own `/usr/lib` or `vendor_libs/`. `objdump` isn't
on MiSTer itself; run this check on a Linux box with `binutils` against
the downloaded file before deploying it.

**Caveat**: this bundle lives under `/media/fat`, which persists across
reboots but is not guaranteed to survive a MiSTer main-binary/OS update —
re-verify (`python3 mister_turing_client.py --once`) after one.

## Alternatives considered for artwork

ScreenScraper is the primary source: IGDB (self-service Twitch signup, but
no CRC matching and weak arcade/MAME coverage) and TheGamesDB (same
manual-forum-request friction as ScreenScraper, no better) were considered
and set aside - every mainstream retro frontend (EmulationStation,
Batocera, Recalbox) independently landed on ScreenScraper for the same
reason: it's the one built for CRC-based, arcade-heavy emulation scraping,
which is what a MiSTer library actually needs.

That said, ScreenScraper's artwork endpoints need a *developer* key
(`ss_dev_user`/`ss_dev_pass`), issued only by manual forum request and
approval - a real bottleneck in practice. `libretro_thumbs.py` is a
fallback source that sidesteps this entirely: it talks to a self-hosted
[`libretro-artwork-api`](https://github.com/kadafi916/libretro-artwork-api)
instance (a small stdlib-only HTTP service, run separately - see that
project's own README) serving a local git-cloned mirror of
[libretro-thumbnails](https://github.com/libretro-thumbnails). No
credentials, no rate limit, no forum. The tradeoff: only box
art/snap/title art per *game* (no wheel art, marquees, fanart, or
system/core-level art for the idle screen - see `libretro_thumbs.py`'s
docstring), and only for whatever systems have actually been
`git clone`d into that service's data directory.

`ss.configured` and `libretro.configured` are independent - either one
alone, both, or neither is a valid setup (artwork just stays off with
neither). With both configured, ScreenScraper is tried first for game
art; libretro-thumbnails is the fallback whenever ScreenScraper is
unconfigured or turns up no match.

## RetroAchievements: reading `unlocks_tracked` correctly

`ra_status.py`'s `unlocks_tracked` field (rendered on the RA Progress page
as "Live-tracking unconfirmed" when false) answers a more specific
question than "is the fork installed" — worth knowing so the message
isn't misread:

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

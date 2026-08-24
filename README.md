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

## Current state / next steps

This renders a first HUD screen: core/game identity (from
`/status/snapshot`), CPU/memory bars and uptime (`/status/system`), and SD
card usage (`/status/storage`). It does not yet do artwork, RetroAchievements,
or multiple pages/touch-equivalent navigation the way the ESP32 firmware
does — this is a first working screen to build on, not feature parity.

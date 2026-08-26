#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""mister_turing_client - MiSTer FPGA status HUD for Turing/UsbMonitor USB
smart screens (turing-smart-screen-python's Rev. A protocol), run
independently of the MiSTer_monitor ESP32 firmware/board_hal path.

Polls mister_status_server.py's JSON HTTP API and renders a HUD with
Pillow, pushed to the screen over the vendored turing_lcd library. See
README.md for the full picture, including why Pillow/numpy are vendored
rather than pip-installed on MiSTer's own Buildroot Python.

Up to four pages rotate automatically (no touch input on this hardware,
--pages/config.ini's [pages] picks which and in what order - a single page
pins the display to just that one, e.g. --pages boxart; config.ini's
[pages_ra] can swap in a different rotation for as long as an RA-adapted
core is active, switching back automatically the instant it isn't):
"Now Playing" (artwork + core/game
identity + system stats), a full-screen "Box Art" page (when artwork is
available), and, only while the *active core* is actually an RA-adapted
one (RA_-prefixed CORENAME, or the odelot fork's live debug-log heartbeat
- see is_ra_core_active()), "RA Progress" and "RA Trophies" (paginated).
A stock core with a hash that merely happens to match something in RA's
database (server-side cloud polling, independent of the core) does not
get RA pages - see is_ra_core_active(). Now Playing, RA Progress and RA
Trophies all show the current game/system artwork alongside their info
(see PAGES_WITH_ART); Box Art is the same artwork full-screen instead. An
achievement unlock shows a popup overlay the moment the server reports
one, on top of whatever page is current.
"""

import hashlib
import os
import sys

# --- Runtime bootstrap -------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PYLIBS = os.path.join(_HERE, "pylibs")
_VENDOR_LIBS = os.path.join(_HERE, "vendor_libs")

if os.environ.get("_MISTER_TURING_RELAUNCHED") != "1":
    env = os.environ.copy()
    env["_MISTER_TURING_RELAUNCHED"] = "1"
    env["PYTHONPATH"] = _PYLIBS + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["LD_LIBRARY_PATH"] = _VENDOR_LIBS + (os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:], env)

sys.path.insert(0, _HERE)
sys.path.insert(0, _PYLIBS)

import argparse
import json
import threading
import time
import urllib.request
import urllib.error

from PIL import Image, ImageDraw, ImageFont

from turing_lcd.lcd_comm import Orientation
from turing_lcd.lcd_comm_rev_a import LcdCommRevA
from turing_lcd.log import logger

from config import Config
from screenscraper import ScreenScraperClient
from libretro_thumbs import LibretroThumbsClient
import screenscraper_systems
import retroachievements as ra

FONT_DIR = os.path.join(_HERE, "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "RobotoMono-Regular.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "RobotoMono-Bold.ttf")
ARTWORK_CACHE_DIR = os.path.join(_HERE, "artwork_cache")

# Palette
BG = (18, 18, 24)
HEADER_BG = (28, 28, 38)
FG = (235, 235, 235)
ACCENT = (90, 170, 255)
DIM = (150, 150, 160)
WARN = (230, 90, 90)
GOOD = (110, 210, 140)
GOLD = (230, 190, 90)
POPUP_BG = (26, 22, 10)

ART_W = 200  # artwork panel width on pages that show one

DEFAULT_PAGE_SECONDS = 25.0
POPUP_SECONDS = 6.0

ALL_PAGES = ("now_playing", "boxart", "ra_summary", "ra_trophies")
# Pages laid out as an info panel + an artwork sidebar (see
# render_art_panel()) - "boxart" isn't one of these, it's art filling the
# whole frame with no info panel to split. Used both to pick the steady-
# state partial-redraw path in main() and to reset that path's dedup
# caches after any full redraw, for every page shaped this way - not just
# Now Playing, now that RA Progress/Trophies show artwork too.
PAGES_WITH_ART = ("now_playing", "ra_summary", "ra_trophies")


def parse_pages(raw, fallback, source) -> list:
    """raw: a comma-separated page-name string, possibly empty ("not
    set"). Falls back to `fallback` (already-validated) when empty, or
    when every name in it is unrecognized - logged, not fatal, since a
    typo in config.ini shouldn't be able to stop the client from starting
    the way a CLI typo (caught by argparse, exits before this is even
    reached) can. `source` is just what the warning names, e.g.
    "config.ini [pages] pages"."""
    if not raw:
        return list(fallback)
    names = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = [p for p in names if p not in ALL_PAGES]
    if unknown:
        logger.warning("%s: unknown page(s) %s - choose from %s, using default instead",
                        source, unknown, list(ALL_PAGES))
        return list(fallback)
    return names or list(fallback)


def parse_seconds(raw, fallback, source) -> float:
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s: %r isn't a number, using default instead", source, raw)
        return fallback


def fetch_json(base_url: str, path: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as e:
        logger.debug("GET %s failed: %s", path, e)
        return None


def fmt_uptime(seconds) -> str:
    seconds = int(seconds or 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def load_art_image(path, size):
    if not path or not os.path.exists(path):
        return None
    try:
        img = Image.open(path).convert("RGB")
        img.thumbnail(size, Image.LANCZOS)
        return img
    except Exception as e:
        logger.debug("Could not open artwork %s: %s", path, e)
        return None


class CachedArtLoader:
    """load_art_image(), memoized to exactly one (path, size) entry - a
    real, measured cost on real MiSTer hardware: decoding + LANCZOS-resizing
    a real cached boxart PNG took ~1.1-1.3s of actual CPU time (dual-core
    ARM, no hardware JPEG/PNG decode). Called unconditionally on every
    poll tick (the naive version this replaces), that's a recurring
    CPU burst roughly every other --interval, indefinitely, for as long as
    any art is showing - not just a one-off cost at game load. One entry
    is enough: this app only ever needs the currently-relevant art loaded,
    never more than one (page, art_path) combination at a time."""

    def __init__(self):
        self._key = None
        self._image = None

    def get(self, path, size):
        key = (path, size)
        if key != self._key:
            self._key = key
            self._image = load_art_image(path, size)
        return self._image


class ArtworkFetcher:
    """Runs one artwork fetch+decode at a time in a background thread, so
    a slow network request or the CPU-bound decode above never blocks the
    main poll/render loop - confirmed to matter on real hardware: a fetch
    lands exactly at game-load time (an identity change), which is also
    when a just-loaded core is already doing its own heaviest work (a real
    PSX session measured at ~50% CPU + 40% io-wait streaming a CHD over
    CIFS) - running synchronously there means stacking a ~1-1.5s CPU burst
    on top of the worst possible moment instead of letting it happen
    quietly in the background while the loop keeps polling/rendering.

    fetch_fn is expected to return the finished, already-decoded result
    (e.g. an (art_path, art_image) tuple) - see main()'s use - so the main
    thread never has to decode anything itself either."""

    def __init__(self):
        self._lock = threading.Lock()
        self._identity = None
        self._result = None
        self._done = False

    def request(self, identity, fetch_fn):
        """Starts a background fetch for `identity` unless one is already
        running or has already completed for this exact identity - so
        redundant calls on unrelated poll ticks (main() calls this once
        per identity change, not per tick, but the guard costs nothing and
        removes any doubt) are free no-ops."""
        with self._lock:
            if self._identity == identity:
                return
            self._identity = identity
            self._result = None
            self._done = False

        def _run():
            result = fetch_fn()
            with self._lock:
                # Only apply it if nothing newer superseded this identity
                # while the fetch was in flight - a stale, late-arriving
                # result for a game the user already left must never
                # clobber whatever's current.
                if self._identity == identity:
                    self._result = result
                    self._done = True

        threading.Thread(target=_run, name="artwork-fetch", daemon=True).start()

    def result_for(self, identity):
        """Returns fetch_fn's return value once `identity`'s fetch has
        completed, else None (still pending, or a different identity is
        now current)."""
        with self._lock:
            if self._identity != identity or not self._done:
                return None
            return self._result


def draw_stat_bar(d, label, pct, y, x0, x1, font):
    pct = max(0.0, min(100.0, pct or 0.0))
    d.text((x0, y), f"{label:<4}", font=font, fill=DIM)
    bx0, by0, bx1, by1 = x0 + 78, y + 3, x1, y + 18
    d.rectangle([bx0, by0, bx1, by1], outline=DIM)
    fill_w = int((bx1 - bx0 - 2) * pct / 100)
    color = WARN if pct >= 85 else ACCENT
    if fill_w > 0:
        d.rectangle([bx0 + 1, by0 + 1, bx0 + 1 + fill_w, by1 - 1], fill=color)


def draw_header(d, width, core, game, fonts):
    f_title, f_body, f_small = fonts
    d.rectangle([0, 0, width, 40], fill=HEADER_BG)
    d.text((10, 4), str(core)[:30], font=f_title, fill=ACCENT)
    if game:
        d.text((10, 24), str(game)[:56], font=f_small, fill=FG)


def render_waiting(width, height, fonts, message) -> Image.Image:
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    bbox = d.textbbox((0, 0), message, font=f_body)
    tw = bbox[2] - bbox[0]
    d.text(((width - tw) // 2, height // 2 - 8), message, font=f_body, fill=WARN)
    d.text((12, height - 20), time.strftime("%H:%M:%S"), font=f_small, fill=DIM)
    return img


def render_now_playing_info(panel_width, height, snapshot, system, storage, ra_status, fonts) -> Image.Image:
    """Same content as the left (non-artwork) side of render_now_playing(),
    as its own standalone image sized to just that panel. Used for the
    Now Playing page's steady-state redraws (see main()): stats/clock
    change almost every tick, but pushing them shouldn't require resending
    the artwork panel sitting next to them - by far the largest payload on
    this page - every single time too. Must stay pixel-identical to
    render_now_playing()'s left side at the same panel_width, since the two
    are pushed to the same screen region interchangeably (a full redraw via
    render_now_playing(), or an incremental one via this function)."""
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (panel_width, height), BG)
    d = ImageDraw.Draw(img)

    core = snapshot.get("system_name") or snapshot.get("core") or "Menu"
    game = snapshot.get("game") or ""
    draw_header(d, panel_width, core, game, fonts)

    text_x0, text_x1 = 12, panel_width - 12
    y = 52
    draw_stat_bar(d, "CPU", system.get("cpu_usage", 0.0), y, text_x0, text_x1, f_body)
    d.text((text_x1 - 44, y), f"{system.get('cpu_usage', 0.0):>4.0f}%", font=f_small, fill=FG)
    y += 24
    draw_stat_bar(d, "MEM", system.get("memory_usage", 0.0), y, text_x0, text_x1, f_body)
    d.text((text_x1 - 44, y), f"{system.get('memory_usage', 0.0):>4.0f}%", font=f_small, fill=FG)
    y += 28

    d.text((text_x0, y), f"UP   {fmt_uptime(system.get('uptime_seconds', 0))}", font=f_small, fill=DIM)
    y += 20

    sd = (storage or {}).get("sd_card") or {}
    if sd:
        d.text((text_x0, y),
                f"SD   {sd.get('used_gb', 0):.0f}/{sd.get('total_gb', 0):.0f}GB "
                f"({sd.get('usage_percent', 0):.0f}%)",
                font=f_small, fill=DIM)
        y += 20

    if ra_status and ra_status.game_matched:
        y += 4
        d.text((text_x0, y), "\U0001F3C6", font=f_small, fill=GOLD)
        d.text((text_x0 + 20, y),
                f"{ra_status.unlocked}/{ra_status.total}  {ra_status.points_earned}/{ra_status.points_total}pt",
                font=f_small, fill=GOLD)

    d.text((12, height - 18), time.strftime("%H:%M"), font=f_small, fill=DIM)
    return img


def render_art_panel(art_image, box_w, box_h) -> Image.Image:
    """The artwork sidebar as its own fixed-size (box_w x box_h) image, so
    it's always the same geometry to push regardless of the actual art's
    (aspect-fit-thumbnailed) size - matches where render_now_playing()
    pastes art_image directly onto the full frame at the same box origin."""
    canvas = Image.new("RGB", (box_w, box_h), BG)
    if art_image is not None:
        canvas.paste(art_image, (0, 0))
    return canvas


def render_now_playing(width, height, snapshot, system, storage, ra_status, art_image, fonts) -> Image.Image:
    """Full-frame Now Playing composite - just render_now_playing_info() +
    render_art_panel() pasted at the same fixed offsets main()'s
    steady-state partial-update path pushes them at independently, so a
    full redraw (page just switched to, or a popup/art-identity change
    forced one) and an incremental one always agree on the exact layout."""
    has_art = art_image is not None
    left_w = (width - ART_W - 24) if has_art else (width - 12)

    img = Image.new("RGB", (width, height), BG)
    img.paste(render_now_playing_info(left_w, height, snapshot, system, storage, ra_status, fonts), (0, 0))
    if has_art:
        art_x = width - ART_W - 12
        img.paste(render_art_panel(art_image, ART_W, height - 60), (art_x, 48))
    return img


def render_boxart_fullscreen(width, height, art_image, fonts) -> Image.Image:
    """Just the box art, filling as much of the screen as possible
    (letterboxed, centered, aspect ratio preserved). Takes an already-
    decoded, already-resized-to-(width, height) image - decoding this at
    full display resolution is real CPU cost (measured on real hardware:
    over a second), so the caller is expected to cache it across ticks
    (see CachedArtLoader) rather than this function re-decoding art_path
    itself on every call."""
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (width, height), (0, 0, 0))
    if art_image is not None:
        ox, oy = (width - art_image.width) // 2, (height - art_image.height) // 2
        img.paste(art_image, (ox, oy))
    else:
        ImageDraw.Draw(img).text((12, 12), "No artwork", font=f_body, fill=DIM)
    return img


def render_ra_summary_info(panel_width, height, ra_status, fonts) -> Image.Image:
    """Same content as the left (non-artwork) side of render_ra_summary(),
    as its own standalone image - see render_now_playing_info()'s
    docstring for why this split exists and how it's used."""
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (panel_width, height), BG)
    d = ImageDraw.Draw(img)
    draw_header(d, panel_width, "RetroAchievements", ra_status.game_title, fonts)

    x1 = panel_width - 12
    y = 56
    d.text((12, y), "Progress", font=f_body, fill=DIM)
    y += 22
    draw_stat_bar(d, "", ra_status.progress_pct, y, 12, x1 - 58, f_body)
    d.text((x1 - 48, y), f"{ra_status.progress_pct:>3.0f}%", font=f_small, fill=FG)
    y += 30
    d.text((12, y), f"Unlocked   {ra_status.unlocked} / {ra_status.total}", font=f_body, fill=FG)
    y += 22
    d.text((12, y), f"Hardcore   {ra_status.unlocked_hardcore}", font=f_body, fill=GOLD)
    y += 26
    d.text((12, y), f"Points     {ra_status.points_earned} / {ra_status.points_total}", font=f_body, fill=FG)
    y += 22
    d.text((12, y), f"Hardcore   {ra_status.points_hardcore} pt", font=f_body, fill=GOLD)
    y += 30

    if not ra_status.unlocks_tracked:
        # Reaching this page at all already means is_ra_core_active()
        # detected an RA-adapted core (see main()) - so unlocks_tracked
        # being False here specifically means the odelot fork's debug-log
        # heartbeat isn't confirming live tracking, most likely because
        # debug=1 isn't set in /media/fat/retroachievements.cfg yet (the
        # detection fell back to the RA_ CORENAME prefix instead).
        d.text((12, y), "Live-tracking unconfirmed - set debug=1 in", font=f_small, fill=DIM)
        y += 16
        d.text((12, y), "retroachievements.cfg to verify unlocks", font=f_small, fill=DIM)

    d.text((12, height - 18), time.strftime("%H:%M"), font=f_small, fill=DIM)
    return img


def render_ra_summary(width, height, ra_status, art_image, fonts) -> Image.Image:
    """Full-frame RA Progress composite - render_ra_summary_info() +
    render_art_panel() at the same fixed offsets render_now_playing() uses,
    for the same steady-state-vs-full-redraw consistency reason."""
    has_art = art_image is not None
    left_w = (width - ART_W - 24) if has_art else (width - 12)
    img = Image.new("RGB", (width, height), BG)
    img.paste(render_ra_summary_info(left_w, height, ra_status, fonts), (0, 0))
    if has_art:
        art_x = width - ART_W - 12
        img.paste(render_art_panel(art_image, ART_W, height - 60), (art_x, 48))
    return img


def render_ra_trophies_info(panel_width, height, ra_status, achievements, page, pages, fonts) -> Image.Image:
    """Same content as the left (non-artwork) side of render_ra_trophies(),
    as its own standalone image - see render_now_playing_info()'s
    docstring for why this split exists and how it's used."""
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (panel_width, height), BG)
    d = ImageDraw.Draw(img)
    draw_header(d, panel_width, "Trophies", f"{ra_status.game_title}  ({page + 1}/{max(pages, 1)})", fonts)

    # Rough monospace char-width estimate at this font size, so a long
    # title doesn't overflow into (or past) the narrower panel when an
    # artwork sidebar is present - same "good enough, not pixel-measured"
    # truncation approach draw_header already uses for core/game names.
    max_title_chars = max(16, (panel_width - 44) // 8)

    y = 48
    row_h = (height - y - 8) / max(len(achievements), 1) if achievements else 0
    for a in achievements:
        color = GOLD if a.hardcore else (GOOD if a.unlocked else DIM)
        mark = "★" if a.hardcore else ("✓" if a.unlocked else "○")
        d.text((12, int(y)), mark, font=f_body, fill=color)
        title = f"{a.title} ({a.points}pt)"
        d.text((32, int(y)), title[:max_title_chars], font=f_small, fill=FG if a.unlocked else DIM)
        y += max(row_h, 18)

    if not achievements:
        if ra_status.total == 0:
            d.text((12, y), "This game has no achievement set.", font=f_body, fill=DIM)
        else:
            d.text((12, y), "Loading achievements...", font=f_body, fill=DIM)

    return img


def render_ra_trophies(width, height, ra_status, achievements, page, pages, art_image, fonts) -> Image.Image:
    """Full-frame RA Trophies composite - render_ra_trophies_info() +
    render_art_panel() at the same fixed offsets render_now_playing() uses,
    for the same steady-state-vs-full-redraw consistency reason."""
    has_art = art_image is not None
    left_w = (width - ART_W - 24) if has_art else (width - 12)
    img = Image.new("RGB", (width, height), BG)
    img.paste(render_ra_trophies_info(left_w, height, ra_status, achievements, page, pages, fonts), (0, 0))
    if has_art:
        art_x = width - ART_W - 12
        img.paste(render_art_panel(art_image, ART_W, height - 60), (art_x, 48))
    return img


def is_ra_core_active(ra_status) -> bool:
    """True only when achievements earned right now would actually be
    banked by an RA-adapted core - not just "the loaded ROM's hash happens
    to match something in RA's database", which is all ra_status.game_matched
    means (mister_status_server polls RA's cloud API server-side by hash,
    independent of which core - stock or RA - is actually running).

    Two signals, either is enough:

      1. ra_status.unlocks_tracked - the odelot fork's own live debug-log
         heartbeat (see ra_status.py's _unlocks_are_tracked() upstream).
         The authoritative signal, but only available with debug=1 set in
         /media/fat/retroachievements.cfg.

      2. /tmp/CORENAME itself RA_-prefixed. mister_status_server strips
         this prefix server-side before exposing core_raw (see
         _read_corename_raw()), so it can't be checked from the exposed
         JSON API - but this client runs directly on the MiSTer, so it can
         just read the file itself. Covers the common case (an RA-adapted
         core is loaded) even without debug=1 set.
    """
    if ra_status and ra_status.unlocks_tracked:
        return True
    try:
        with open("/tmp/CORENAME") as f:
            corename = f.read().strip()
    except OSError:
        return False
    return corename.upper().startswith("RA_")


def render_unlock_popup(base_img: Image.Image, ra_status, fonts) -> Image.Image:
    width, height = base_img.size
    img = base_img.copy()
    d = ImageDraw.Draw(img)

    box_w, box_h = int(width * 0.82), 96
    x0, y0 = (width - box_w) // 2, (height - box_h) // 2
    x1, y1 = x0 + box_w, y0 + box_h

    d.rectangle([x0, y0, x1, y1], fill=POPUP_BG, outline=GOLD, width=2)
    f_title, f_body, f_small = fonts
    d.text((x0 + 12, y0 + 8), "\U0001F3C6 ACHIEVEMENT UNLOCKED", font=f_small, fill=GOLD)
    d.text((x0 + 12, y0 + 26), ra_status.last_unlock_title[:40], font=f_title, fill=FG)
    pts_label = f"{ra_status.last_unlock_points} pt" + (" (hardcore)" if ra_status.last_unlock_hardcore else "")
    d.text((x0 + 12, y0 + 50), pts_label, font=f_small, fill=GOLD)
    if ra_status.last_unlock_description:
        d.text((x0 + 12, y0 + 68), ra_status.last_unlock_description[:56], font=f_small, fill=DIM)
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default="http://127.0.0.1:8081",
                     help="mister_status_server base URL (default: %(default)s)")
    ap.add_argument("--port", default="AUTO",
                     help="serial port for the screen, e.g. /dev/ttyACM0, or AUTO (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=2.0,
                     help="poll/refresh interval in seconds (default: %(default)s)")
    ap.add_argument("--brightness", type=int, default=80,
                     help="screen brightness 0-100 (default: %(default)s)")
    ap.add_argument("--page-seconds", type=float, default=None,
                     help="how long each page stays up before rotating to the next "
                          "one in --pages - overrides config.ini's [pages]/[pages_ra] "
                          "page_seconds unconditionally when given (default: "
                          f"{DEFAULT_PAGE_SECONDS} if config.ini doesn't set one either)")
    ap.add_argument("--pages", default=None,
                     help="comma-separated pages to rotate through, in rotation "
                          "order - choose from now_playing, boxart, ra_summary, "
                          "ra_trophies. A single page pins the display to just "
                          "that one - e.g. --pages boxart for a box-art-only mode, "
                          "or --pages ra_summary,ra_trophies to skip Now Playing/Box "
                          "Art entirely. Overrides config.ini's [pages]/[pages_ra] "
                          "pages unconditionally when given, disabling the RA/non-RA "
                          f"switching described there (default: {','.join(ALL_PAGES)} "
                          "if config.ini doesn't set one either)")
    ap.add_argument("--config", default=os.path.join(_HERE, "config.ini"),
                     help="path to config.ini (default: %(default)s)")
    ap.add_argument("--once", action="store_true",
                     help="render a single frame and exit (for testing)")
    args = ap.parse_args()

    if args.pages is not None:
        # --pages given: unconditional override, same behavior in every
        # session regardless of RA state - see the help text above.
        pages = parse_pages(args.pages, ALL_PAGES, "--pages")
        pages_ra = None
    else:
        pages = None  # resolved below, once config.ini is loaded
        pages_ra = None

    page_seconds_cli = args.page_seconds  # None unless explicitly given

    config = Config(args.config)

    if pages is None:
        pages = parse_pages(config.pages, ALL_PAGES, "config.ini [pages] pages")
        # [pages_ra] only takes effect if it actually names at least one
        # page - an empty/absent section means "no RA-specific override",
        # not "empty rotation" (see Config's own docstring-equivalent
        # comment in config.py).
        if config.pages_ra:
            pages_ra = parse_pages(config.pages_ra, pages, "config.ini [pages_ra] pages")

    if page_seconds_cli is not None:
        page_seconds = page_seconds_cli
        page_seconds_ra = None
    else:
        page_seconds = parse_seconds(config.page_seconds, DEFAULT_PAGE_SECONDS, "config.ini [pages] page_seconds")
        page_seconds_ra = (parse_seconds(config.page_seconds_ra, page_seconds, "config.ini [pages_ra] page_seconds")
                            if pages_ra is not None else None)

    ss = ScreenScraperClient(config, ARTWORK_CACHE_DIR)
    libretro = LibretroThumbsClient(config.libretro_base_url, ARTWORK_CACHE_DIR)
    if not ss.configured and not libretro.configured:
        logger.info("No artwork source configured (config.ini) - artwork disabled")
    elif not ss.configured:
        logger.info("ScreenScraper not configured (config.ini) - using libretro-thumbnails fallback only")
    unlock_tracker = ra.UnlockTracker()

    logger.info("Opening display on %s ...", args.port)
    comm = LcdCommRevA(com_port=args.port, display_width=320, display_height=480)
    comm.InitializeComm()
    comm.SetBrightness(args.brightness)
    comm.Clear()
    comm.SetOrientation(Orientation.LANDSCAPE)
    width, height = comm.get_width(), comm.get_height()
    logger.info("Display ready: %dx%d", width, height)

    fonts = (
        ImageFont.truetype(FONT_BOLD, 20),
        ImageFont.truetype(FONT_REGULAR, 16),
        ImageFont.truetype(FONT_REGULAR, 13),
    )

    page_idx = 0
    page_deadline = 0.0
    ra_mode_active = None  # None = not yet evaluated; forces the first tick to "switch"
    trophies_page_num = 0
    trophies_total_pages = 1
    art_identity = None  # (system_id, crc-or-name) of the art currently cached
    pending_identity = None  # a not-yet-confirmed identity - see the debounce below
    art_path = None
    art_image = None
    art_fetcher = ArtworkFetcher()
    boxart_loader = CachedArtLoader()  # full-resolution decode for the Box Art page
    popup_until = 0.0
    # Carried across an "identity_unconfirmed" tick (see below) so the very
    # first poll - unlikely but possible - has something defined.
    core_raw = ""
    system_id = ""
    has_game = False
    libretro_system = ""
    identity = (None, "__system__")
    media_order = ""

    # Steady-state redraw dedup (see the "transition" logic in the loop
    # below) - what's believed to currently be physically on screen.
    last_page_shown = None
    last_full_hash = None       # ra_summary / ra_trophies / boxart (full-frame pages)
    info_hash = None            # now_playing steady-state: info panel content
    art_pushed_identity = None  # now_playing steady-state: art panel identity

    try:
        while True:
            now = time.time()
            snapshot = fetch_json(args.server, "/status/snapshot")
            connected = snapshot is not None

            if not connected:
                comm.DisplayPILImage(render_waiting(width, height, fonts,
                                                      "Waiting for MiSTer status server..."))
                # Invalidate the steady-state dedup cache: the screen now
                # shows this waiting message, not whatever page it looked
                # like last tick, so the next real page render must be a
                # full redraw regardless of whether that page "changed".
                last_page_shown = None
                if args.once:
                    break
                time.sleep(args.interval)
                continue

            system = fetch_json(args.server, "/status/system") or {}
            storage = fetch_json(args.server, "/status/storage") or {}
            rom_details = fetch_json(args.server, "/status/rom/details") or {}
            ra_status = ra.fetch_status(fetch_json, args.server)
            new_unlock = unlock_tracker.check(ra_status)

            # -- artwork: fetch once per identity change, cache does the rest --
            # mister_status_server can report rom_details with
            # detection_method "identity_unconfirmed" - a deliberate,
            # documented transient (its own source: "the OSD cursor is
            # resting on another title while the loaded game keeps
            # running... transient by nature, never cached") that reports
            # available=False even though a game is still genuinely
            # running. Treating that as "no game loaded" - which
            # has_game's naive available-flag check would do - spuriously
            # wipes whatever art/identity was already correctly showing:
            # confirmed real symptom, art loaded correctly then vanished
            # on the very next poll. So on this tick specifically, skip
            # recomputing identity/has_game/etc entirely and keep
            # whatever was last established - core_raw/system_id/has_game
            # simply keep their previous values (see their pre-loop
            # initialization above).
            if rom_details.get("detection_method") != "identity_unconfirmed":
                core_raw = snapshot.get("core_raw") or ""
                system_name = rom_details.get("artwork_system") or snapshot.get("system_name") or ""
                system_id = screenscraper_systems.get_system_id(system_name, core_raw)
                has_game = bool(snapshot.get("game")) and rom_details.get("available")

                # A genuinely RA-active core (the odelot fork, launched
                # directly rather than via MiSTer Companion) reports
                # core_raw with its "RA_" prefix intact - confirmed via
                # real logs: "RA_NES" with unlocks_tracked=true and
                # log_tail=true (definitely the real fork running), yet
                # "unmapped system: ra_nes" from the artwork API.
                # screenscraper_systems's id resolution tolerates this by
                # falling back to system_name ("Nintendo NES/Famicom" -> id
                # 3), but libretro_thumbs.py's API only understands
                # stock core_raw-shaped keys - strip it here the same way
                # mister_status_server.py's own _read_corename_raw() does
                # server-side for the fields that DO get stripped.
                _stock_core_raw = core_raw[3:] if core_raw.upper().startswith("RA_") else core_raw

                # For arcade content, MiSTer's core_raw is the *loaded
                # .mra's own setname* ("rtype", "tmnt2", "invaders", ...) -
                # a different value per game, never a stable "this is
                # arcade" identifier (confirmed via real logs: every arcade
                # game failed with "unmapped system: <its own setname>").
                # screenscraper_systems's id resolution already tolerates
                # this by falling back to system_name ("Arcade" -> id 75),
                # but libretro_thumbs.py's API only understands
                # core_raw-shaped keys - so override it here using the
                # snapshot's own is_arcade flag, verified against the real
                # artwork API to resolve arcade titles correctly.
                libretro_system = "arcade" if snapshot.get("is_arcade") else _stock_core_raw

                if has_game:
                    identity = (system_id, rom_details.get("crc32") or rom_details.get("search_name") or "")
                    media_order = config.arcade_media_order if snapshot.get("is_arcade") else config.game_media_order
                else:
                    identity = (system_id, "__system__")
                    media_order = (config.arcade_subsystem_media_order if snapshot.get("is_arcade")
                                    else config.core_media_order)

            # libretro-thumbnails only ever has *game* art (see
            # libretro_thumbs.py's docstring) - so with ScreenScraper
            # unconfigured (as it is right now: no dev key yet), there is
            # nothing to fetch at all for the no-game case (Menu, or a core
            # loaded with nothing in it).
            if has_game:
                will_fetch_art = (ss.configured and system_id) or libretro.configured
            else:
                will_fetch_art = ss.configured and system_id

            force_full_redraw = False
            # Debounced: a new identity only gets acted on once it's been
            # seen on two consecutive polls, not the first time it
            # appears. Confirmed happening in practice and not caught by
            # the identity_unconfirmed handling above (a different,
            # explicitly-flagged transient - this one wasn't flagged as
            # uncertain, it was reported as a confident, real load): OSD
            # ROM browsing on a stock SNES core briefly, confidently
            # reported a highlighted-but-never-loaded game as the active
            # one, and this client dutifully fetched and cached real
            # artwork for it (Earthbound) before the actually-loaded game
            # (Yoshi's Island) showed up ~26s later. mister_status_server's
            # own detection logic already has extensive empirical
            # navigation-vs-launch hardening (FILESELECT/CURRENTPATH/
            # ACTIVEGAME mtime comparisons - see its source), so this is a
            # rare edge it doesn't fully close rather than something
            # obviously wrong there; a one-poll debounce here defends
            # against this and any similar not-yet-discovered case
            # generally, at the cost of a ~1 poll interval (--interval,
            # 2s default) delay before art fetches for a genuine load too
            # - imperceptible, especially now that fetching never blocks
            # rendering anyway (see ArtworkFetcher).
            if identity == art_identity:
                pending_identity = None
            elif identity != pending_identity:
                pending_identity = identity
            else:
                pending_identity = None
                art_identity = identity
                art_path = None
                art_image = None
                force_full_redraw = True  # see the transition comment below
                if will_fetch_art:
                    # Runs in a background thread (see ArtworkFetcher) -
                    # never blocks this loop, which keeps polling/rendering
                    # normally (without art, until the fetch completes)
                    # instead of freezing on a "Downloading artwork..."
                    # screen. Real measurement on real hardware: fetch +
                    # decode can cost ~1.5s wall / ~1.2s CPU, and it lands
                    # exactly at game-load time - the same moment a
                    # just-loaded core (PSX especially) is already doing
                    # its own heaviest work, so blocking here means
                    # stacking a CPU burst on the worst possible moment
                    # instead of letting it happen quietly in the
                    # background. ctx snapshots every per-tick value the
                    # worker needs as of *this* identity change - the loop's
                    # own locals get reassigned every tick and would
                    # otherwise race with whatever the background thread
                    # reads once it actually runs.
                    ctx = {
                        "has_game": has_game,
                        "ss_configured": ss.configured,
                        "system_id": system_id,
                        "rom_details": rom_details,
                        "media_order": media_order,
                        "libretro_configured": libretro.configured,
                        "libretro_system": libretro_system,
                        "title": rom_details.get("search_name") or snapshot.get("game") or "",
                        "cache_key": rom_details.get("crc32") or rom_details.get("search_name") or "",
                    }

                    def _fetch_and_decode(ctx=ctx):
                        path = None
                        if ctx["has_game"]:
                            if ctx["ss_configured"] and ctx["system_id"]:
                                path = ss.fetch_game_art(ctx["system_id"], ctx["rom_details"], ctx["media_order"])
                            if not path and ctx["libretro_configured"]:
                                # Fallback: no credentials, no rate limit,
                                # but game art only - see libretro_thumbs.py.
                                path = libretro.fetch_game_art(ctx["libretro_system"], ctx["cache_key"],
                                                                ctx["title"], ctx["media_order"])
                        elif ctx["ss_configured"] and ctx["system_id"]:
                            # libretro-thumbnails has no system/core-level
                            # art equivalent - see libretro_thumbs.py's
                            # docstring.
                            path = ss.fetch_system_art(ctx["system_id"], ctx["system_id"], ctx["media_order"])
                        return path, load_art_image(path, (ART_W, height - 60))

                    art_fetcher.request(identity, _fetch_and_decode)

            fetched = art_fetcher.result_for(art_identity)
            if fetched is not None:
                art_path, art_image = fetched

            # -- page selection --
            # RA pages require the *active core* to actually be RA-adapted -
            # not just "the loaded ROM's hash happens to match something in
            # RA's database", which is all game_matched means on its own
            # (server-side cloud polling, independent of core) - see
            # is_ra_core_active().
            core_ra_active = is_ra_core_active(ra_status)
            ra_pages_available = ra_status.game_matched and core_ra_active
            has_art_page = art_path is not None

            # config.ini's [pages_ra] (see config.ini.example) swaps in a
            # different rotation/timing for as long as an RA-adapted core
            # is genuinely active - independent of ra_pages_available
            # above, which additionally requires a matched game: the
            # intent of a config like "cover art only normally, full
            # rotation the moment I'm on an RA core" is "I'm using an
            # RA-capable core", not "this specific game already resolved",
            # so this switches on the core alone.
            use_ra_mode = pages_ra is not None and core_ra_active
            if use_ra_mode != ra_mode_active:
                # Mode just changed - page_idx may not even be a valid
                # index into the newly-active list, and whatever's left of
                # the old page_deadline shouldn't apply to it either.
                # Force an immediate fresh selection from the new list.
                ra_mode_active = use_ra_mode
                page_idx = -1
                page_deadline = 0.0
            active_pages = pages_ra if use_ra_mode else pages
            active_page_seconds = (page_seconds_ra if use_ra_mode and page_seconds_ra is not None
                                    else page_seconds)

            def _page_available(name):
                if name in ("ra_summary", "ra_trophies"):
                    return ra_pages_available
                if name == "boxart":
                    return has_art_page
                return True

            if now >= page_deadline:
                for _ in range(len(active_pages)):
                    page_idx = (page_idx + 1) % len(active_pages)
                    if _page_available(active_pages[page_idx]):
                        break
                page_deadline = now + active_page_seconds
                if active_pages[page_idx] == "ra_trophies":
                    trophies_page_num = (trophies_page_num + 1) % trophies_total_pages

            page = active_pages[page_idx]

            if new_unlock:
                popup_until = now + POPUP_SECONDS
            show_popup = now < popup_until

            # A "transition" - the page just changed, a popup needs to be
            # shown/refreshed, or the art identity just changed underneath
            # us (force_full_redraw) - always gets a full, unconditional
            # redraw. Anything else reaching the loop again with the *same*
            # page as last tick is a steady-state refresh, handled below
            # with per-region dedup so an unmoving screen doesn't keep
            # resending identical content over this display's slow serial
            # link (the actual root cause of the "changed too much" /
            # "half the art visible" symptoms - Now Playing's artwork was,
            # unconditionally, part of every single poll-interval redraw).
            transition = (page != last_page_shown) or show_popup or force_full_redraw or args.once

            # ra_trophies needs a fresh achievements fetch regardless of
            # which redraw path runs below - it's cheap (ra_status.py's own
            # docs: "costs ZERO extra RA API calls", derived from data the
            # progress fetch already downloads) and unlocks can land while
            # sitting on this page.
            if page == "ra_trophies":
                achievements, cur_page, total_pages = ra.fetch_achievements_page(
                    fetch_json, args.server, trophies_page_num)
                trophies_total_pages = max(total_pages, 1)

            if page in PAGES_WITH_ART and not transition:
                has_art = art_image is not None
                left_w = (width - ART_W - 24) if has_art else (width - 12)

                if page == "now_playing":
                    info_img = render_now_playing_info(left_w, height, snapshot, system, storage, ra_status, fonts)
                elif page == "ra_summary":
                    info_img = render_ra_summary_info(left_w, height, ra_status, fonts)
                else:  # ra_trophies
                    info_img = render_ra_trophies_info(left_w, height, ra_status, achievements,
                                                        cur_page, total_pages, fonts)

                h = hashlib.blake2b(info_img.tobytes(), digest_size=16).digest()
                if h != info_hash:
                    comm.DisplayPILImage(info_img, x=0, y=0, image_width=left_w, image_height=height)
                    info_hash = h
                if has_art and art_identity != art_pushed_identity:
                    art_x = width - ART_W - 12
                    art_canvas = render_art_panel(art_image, ART_W, height - 60)
                    comm.DisplayPILImage(art_canvas, x=art_x, y=48, image_width=ART_W, image_height=height - 60)
                    art_pushed_identity = art_identity
            else:
                if page == "now_playing":
                    frame = render_now_playing(width, height, snapshot, system, storage, ra_status, art_image, fonts)
                elif page == "ra_summary":
                    frame = render_ra_summary(width, height, ra_status, art_image, fonts)
                elif page == "ra_trophies":
                    frame = render_ra_trophies(width, height, ra_status, achievements, cur_page, total_pages,
                                                art_image, fonts)
                else:  # boxart
                    boxart_image = boxart_loader.get(art_path, (width, height))
                    frame = render_boxart_fullscreen(width, height, boxart_image, fonts)

                if show_popup:
                    frame = render_unlock_popup(frame, ra_status, fonts)

                h = hashlib.blake2b(frame.tobytes(), digest_size=16).digest()
                if transition or h != last_full_hash:
                    try:
                        comm.DisplayPILImage(frame)
                    except Exception as e:
                        logger.error("Display update failed: %s", e)
                last_full_hash = h

                if page in PAGES_WITH_ART:
                    # This tick's full composite already painted the
                    # current info + art content - seed both steady-state
                    # caches so the next tick's partial-update path doesn't
                    # immediately re-push what's already correctly on screen.
                    info_hash = None  # differs in shape from info_img's own hash; let it recompute once, cheap
                    art_pushed_identity = art_identity

            last_page_shown = page

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Interrupted, closing...")
    finally:
        comm.closeSerial()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""mister_turing_client - MiSTer FPGA status HUD for Turing/UsbMonitor USB
smart screens (turing-smart-screen-python's Rev. A protocol), run
independently of the MiSTer_monitor ESP32 firmware/board_hal path.

Polls mister_status_server.py's JSON HTTP API and renders a HUD with
Pillow, pushed to the screen over the vendored turing_lcd library. See
README.md for the full picture, including why Pillow/numpy are vendored
rather than pip-installed on MiSTer's own Buildroot Python.

Up to four pages rotate automatically (no touch input on this hardware):
"Now Playing" (artwork + core/game identity + system stats), a full-screen
"Box Art" page (when artwork is available), and, only while the *active
core* is actually an RA-adapted one (RA_-prefixed CORENAME, or the odelot
fork's live debug-log heartbeat - see is_ra_core_active()), "RA Progress"
and "RA Trophies" (paginated). A stock core with a hash that merely
happens to match something in RA's database (server-side cloud polling,
independent of the core) does not get RA pages - see is_ra_core_active().
An achievement unlock shows a popup overlay the moment the server reports
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

ART_W = 200  # artwork panel width on the Now Playing page

DEFAULT_PAGE_SECONDS = 12.0
POPUP_SECONDS = 6.0


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


def render_boxart_fullscreen(width, height, art_path, fonts) -> Image.Image:
    """Just the box art, filling as much of the screen as possible
    (letterboxed, centered, aspect ratio preserved) - reloads art_path at
    full display resolution rather than reusing the small ART_W sidebar
    thumbnail already held for the Now Playing page."""
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (width, height), (0, 0, 0))
    full = load_art_image(art_path, (width, height))
    if full is not None:
        ox, oy = (width - full.width) // 2, (height - full.height) // 2
        img.paste(full, (ox, oy))
    else:
        ImageDraw.Draw(img).text((12, 12), "No artwork", font=f_body, fill=DIM)
    return img


def render_ra_summary(width, height, ra_status, fonts) -> Image.Image:
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    draw_header(d, width, "RetroAchievements", ra_status.game_title, fonts)

    y = 56
    d.text((12, y), "Progress", font=f_body, fill=DIM)
    y += 22
    draw_stat_bar(d, "", ra_status.progress_pct, y, 12, width - 70, f_body)
    d.text((width - 60, y), f"{ra_status.progress_pct:>3.0f}%", font=f_small, fill=FG)
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


def render_ra_trophies(width, height, ra_status, achievements, page, pages, fonts) -> Image.Image:
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    draw_header(d, width, "Trophies", f"{ra_status.game_title}  ({page + 1}/{max(pages, 1)})", fonts)

    y = 48
    row_h = (height - y - 8) / max(len(achievements), 1) if achievements else 0
    for a in achievements:
        color = GOLD if a.hardcore else (GOOD if a.unlocked else DIM)
        mark = "★" if a.hardcore else ("✓" if a.unlocked else "○")
        d.text((12, int(y)), mark, font=f_body, fill=color)
        title = f"{a.title} ({a.points}pt)"
        d.text((32, int(y)), title[:52], font=f_small, fill=FG if a.unlocked else DIM)
        y += max(row_h, 18)

    if not achievements:
        if ra_status.total == 0:
            d.text((12, y), "This game has no achievement set.", font=f_body, fill=DIM)
        else:
            d.text((12, y), "Loading achievements...", font=f_body, fill=DIM)

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
    ap.add_argument("--page-seconds", type=float, default=DEFAULT_PAGE_SECONDS,
                     help="how long each page (Now Playing / Box Art / RA Progress / "
                          "RA Trophies) stays up before rotating (default: %(default)s)")
    ap.add_argument("--config", default=os.path.join(_HERE, "config.ini"),
                     help="path to config.ini (default: %(default)s)")
    ap.add_argument("--once", action="store_true",
                     help="render a single frame and exit (for testing)")
    args = ap.parse_args()

    config = Config(args.config)
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

    pages = ["now_playing", "boxart", "ra_summary", "ra_trophies"]
    page_idx = 0
    page_deadline = 0.0
    trophies_page_num = 0
    trophies_total_pages = 1
    art_identity = None  # (system_id, crc-or-name) of the art currently cached
    art_path = None
    popup_until = 0.0

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
            core_raw = snapshot.get("core_raw") or ""
            system_name = rom_details.get("artwork_system") or snapshot.get("system_name") or ""
            system_id = screenscraper_systems.get_system_id(system_name, core_raw)
            has_game = bool(snapshot.get("game")) and rom_details.get("available")

            # For arcade content, MiSTer's core_raw is the *loaded .mra's own
            # setname* ("rtype", "tmnt2", "invaders", ...) - a different
            # value per game, never a stable "this is arcade" identifier
            # (confirmed via real logs: every arcade game failed with
            # "unmapped system: <its own setname>"). screenscraper_systems's
            # id resolution already tolerates this by falling back to
            # system_name ("Arcade" -> id 75), but libretro_thumbs.py's API
            # only understands core_raw-shaped keys - so override it here
            # using the snapshot's own is_arcade flag, verified against the
            # real artwork API to resolve arcade titles correctly.
            libretro_system = "arcade" if snapshot.get("is_arcade") else core_raw

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
            # loaded with nothing in it), and showing "Downloading
            # artwork..." there would be pure noise implying work that was
            # never going to happen.
            if has_game:
                will_fetch_art = (ss.configured and system_id) or libretro.configured
            else:
                will_fetch_art = ss.configured and system_id

            force_full_redraw = False
            if identity != art_identity:
                art_identity = identity
                art_path = None
                force_full_redraw = True  # see the transition comment below
                if will_fetch_art:
                    comm.DisplayPILImage(render_waiting(width, height, fonts, "Downloading artwork..."))
                    if has_game:
                        if ss.configured and system_id:
                            art_path = ss.fetch_game_art(system_id, rom_details, media_order)
                        if not art_path and libretro.configured:
                            # Fallback: no credentials, no rate limit, but
                            # game art only - see libretro_thumbs.py.
                            title = rom_details.get("search_name") or snapshot.get("game") or ""
                            cache_key = rom_details.get("crc32") or rom_details.get("search_name") or ""
                            art_path = libretro.fetch_game_art(libretro_system, cache_key, title, media_order)
                    elif ss.configured and system_id:
                        # libretro-thumbnails has no system/core-level art
                        # equivalent - see libretro_thumbs.py's docstring.
                        art_path = ss.fetch_system_art(system_id, system_id, media_order)

            art_image = load_art_image(art_path, (ART_W, height - 60))

            # -- page selection --
            # RA pages require the *active core* to actually be RA-adapted -
            # not just "the loaded ROM's hash happens to match something in
            # RA's database", which is all game_matched means on its own
            # (server-side cloud polling, independent of core) - see
            # is_ra_core_active().
            ra_pages_available = ra_status.game_matched and is_ra_core_active(ra_status)
            has_art_page = art_path is not None

            def _page_available(name):
                if name in ("ra_summary", "ra_trophies"):
                    return ra_pages_available
                if name == "boxart":
                    return has_art_page
                return True

            if now >= page_deadline:
                for _ in range(len(pages)):
                    page_idx = (page_idx + 1) % len(pages)
                    if _page_available(pages[page_idx]):
                        break
                page_deadline = now + args.page_seconds
                if pages[page_idx] == "ra_trophies":
                    trophies_page_num = (trophies_page_num + 1) % trophies_total_pages

            page = pages[page_idx]

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

            if page == "now_playing" and not transition:
                has_art = art_image is not None
                left_w = (width - ART_W - 24) if has_art else (width - 12)
                info_img = render_now_playing_info(left_w, height, snapshot, system, storage, ra_status, fonts)
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
                    frame = render_ra_summary(width, height, ra_status, fonts)
                elif page == "ra_trophies":
                    achievements, cur_page, total_pages = ra.fetch_achievements_page(
                        fetch_json, args.server, trophies_page_num)
                    trophies_total_pages = max(total_pages, 1)
                    frame = render_ra_trophies(width, height, ra_status, achievements, cur_page, total_pages, fonts)
                else:  # boxart
                    frame = render_boxart_fullscreen(width, height, art_path, fonts)

                if show_popup:
                    frame = render_unlock_popup(frame, ra_status, fonts)

                h = hashlib.blake2b(frame.tobytes(), digest_size=16).digest()
                if transition or h != last_full_hash:
                    try:
                        comm.DisplayPILImage(frame)
                    except Exception as e:
                        logger.error("Display update failed: %s", e)
                last_full_hash = h

                if page == "now_playing":
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

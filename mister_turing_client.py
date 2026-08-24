#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""mister_turing_client - MiSTer FPGA status HUD for Turing/UsbMonitor USB
smart screens (turing-smart-screen-python's Rev. A protocol), run
independently of the MiSTer_monitor ESP32 firmware/board_hal path.

Polls mister_status_server.py's JSON HTTP API and renders a HUD with
Pillow, pushed to the screen over the vendored turing_lcd library. See
README.md for the full picture, including why Pillow/numpy are vendored
rather than pip-installed on MiSTer's own Buildroot Python.

Three pages rotate automatically (no touch input on this hardware):
"Now Playing" (artwork + core/game identity + system stats), and, when
RetroAchievements is configured and matched a game, "RA Progress" and
"RA Trophies" (paginated). A ResourceEvent unlock shows a popup overlay
the moment the server reports one, on top of whatever page is current.
"""

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

PAGE_SECONDS = 6.0
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


def render_now_playing(width, height, snapshot, system, storage, ra_status, art_image, fonts) -> Image.Image:
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    core = snapshot.get("system_name") or snapshot.get("core") or "Menu"
    game = snapshot.get("game") or ""
    draw_header(d, width, core, game, fonts)

    text_x0 = 12
    if art_image is not None:
        art_x = width - art_image.width - 12
        art_y = 48
        img.paste(art_image, (art_x, art_y))
        text_x1 = art_x - 12
    else:
        text_x1 = width - 12

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

    d.text((12, height - 18), time.strftime("%H:%M:%S"), font=f_small, fill=DIM)
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
        d.text((12, y), "View-only (stock MiSTer) - pair with", font=f_small, fill=DIM)
        y += 16
        d.text((12, y), "odelot/Main_MiSTer to record unlocks", font=f_small, fill=DIM)

    d.text((12, height - 18), time.strftime("%H:%M:%S"), font=f_small, fill=DIM)
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
        d.text((12, y), "No achievements loaded yet.", font=f_body, fill=DIM)

    return img


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
    ap.add_argument("--config", default=os.path.join(_HERE, "config.ini"),
                     help="path to config.ini (default: %(default)s)")
    ap.add_argument("--once", action="store_true",
                     help="render a single frame and exit (for testing)")
    args = ap.parse_args()

    config = Config(args.config)
    ss = ScreenScraperClient(config, ARTWORK_CACHE_DIR)
    if not ss.configured:
        logger.info("ScreenScraper not configured (config.ini) - artwork disabled")
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

    pages = ["now_playing", "ra_summary", "ra_trophies"]
    page_idx = 0
    page_deadline = 0.0
    trophies_page_num = 0
    trophies_total_pages = 1
    art_identity = None  # (system_id, crc-or-name) of the art currently cached
    art_path = None
    popup_until = 0.0

    try:
        while True:
            now = time.time()
            snapshot = fetch_json(args.server, "/status/snapshot")
            connected = snapshot is not None

            if not connected:
                comm.DisplayPILImage(render_waiting(width, height, fonts,
                                                      "Waiting for MiSTer status server..."))
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

            if has_game:
                identity = (system_id, rom_details.get("crc32") or rom_details.get("search_name") or "")
                media_order = config.arcade_media_order if snapshot.get("is_arcade") else config.game_media_order
            else:
                identity = (system_id, "__system__")
                media_order = (config.arcade_subsystem_media_order if snapshot.get("is_arcade")
                                else config.core_media_order)

            if identity != art_identity:
                art_identity = identity
                art_path = None
                if ss.configured and system_id:
                    comm.DisplayPILImage(render_waiting(width, height, fonts, "Downloading artwork..."))
                    if has_game:
                        art_path = ss.fetch_game_art(system_id, rom_details, media_order)
                    else:
                        art_path = ss.fetch_system_art(system_id, system_id, media_order)

            art_image = load_art_image(art_path, (ART_W, height - 60))

            # -- page selection --
            ra_pages_available = ra_status.game_matched
            if now >= page_deadline:
                page_idx = (page_idx + 1) % len(pages)
                if pages[page_idx] != "now_playing" and not ra_pages_available:
                    page_idx = 0
                page_deadline = now + PAGE_SECONDS
                if pages[page_idx] == "ra_trophies":
                    trophies_page_num = (trophies_page_num + 1) % trophies_total_pages

            page = pages[page_idx]
            if page == "now_playing":
                frame = render_now_playing(width, height, snapshot, system, storage, ra_status, art_image, fonts)
            elif page == "ra_summary":
                frame = render_ra_summary(width, height, ra_status, fonts)
            else:
                achievements, cur_page, total_pages = ra.fetch_achievements_page(
                    fetch_json, args.server, trophies_page_num)
                trophies_total_pages = max(total_pages, 1)
                frame = render_ra_trophies(width, height, ra_status, achievements, cur_page, total_pages, fonts)

            if new_unlock:
                popup_until = now + POPUP_SECONDS
            if now < popup_until:
                frame = render_unlock_popup(frame, ra_status, fonts)

            try:
                comm.DisplayPILImage(frame)
            except Exception as e:
                logger.error("Display update failed: %s", e)

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Interrupted, closing...")
    finally:
        comm.closeSerial()


if __name__ == "__main__":
    main()

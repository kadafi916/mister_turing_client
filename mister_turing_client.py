#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""mister_turing_client - MiSTer FPGA status HUD for Turing/UsbMonitor USB
smart screens (turing-smart-screen-python's Rev. A protocol), run
independently of the MiSTer_monitor ESP32 firmware/board_hal path.

Polls mister_status_server.py's JSON HTTP API (the same hardware-agnostic
server the MiSTer_monitor ESP32 firmware talks to - see
MiSTer_monitor/MiSTer/Scripts/.config/mister_monitor/mister_status_server.py)
and renders a HUD with Pillow, pushed to the screen over the vendored
turing_lcd library.

Designed to run directly on MiSTer's own Buildroot Python 3.9 (armv7l),
using a prebuilt Pillow/numpy wheel pair plus a handful of vendored .so
files rather than a native build - see README.md for how that bundle is
assembled and why it's needed on this platform.
"""

import os
import sys

# --- Runtime bootstrap -------------------------------------------------
# LD_LIBRARY_PATH is read by the dynamic linker at process exec time, so it
# can't be set from inside an already-running interpreter - we have to
# relaunch ourselves once with it (and PYTHONPATH) in place. This makes the
# script runnable directly (`python3 mister_turing_client.py ...`) without
# requiring a separate wrapper shell script to set the environment first.
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

# --- Everything below only imports once the relaunch above has happened ---
import argparse
import json
import time
import urllib.request
import urllib.error

from PIL import Image, ImageDraw, ImageFont

from turing_lcd.lcd_comm import Orientation
from turing_lcd.lcd_comm_rev_a import LcdCommRevA
from turing_lcd.log import logger

FONT_DIR = os.path.join(_HERE, "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "RobotoMono-Regular.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "RobotoMono-Bold.ttf")

# Palette
BG = (18, 18, 24)
HEADER_BG = (28, 28, 38)
FG = (235, 235, 235)
ACCENT = (90, 170, 255)
DIM = (150, 150, 160)
WARN = (230, 90, 90)


def fetch_json(base_url: str, path: str, timeout: float = 2.0):
    """GET base_url+path and parse it as JSON. Returns None on any failure
    (server not running yet, network hiccup, bad JSON, ...) rather than
    raising - callers render a "waiting" state instead of crashing."""
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


def draw_stat_bar(d: ImageDraw.ImageDraw, label: str, pct: float, y: int, width: int, font):
    pct = max(0.0, min(100.0, pct or 0.0))
    d.text((12, y), f"{label:<4}", font=font, fill=DIM)
    bx0, by0, bx1, by1 = 90, y + 3, width - 70, y + 18
    d.rectangle([bx0, by0, bx1, by1], outline=DIM)
    fill_w = int((bx1 - bx0 - 2) * pct / 100)
    color = WARN if pct >= 85 else ACCENT
    if fill_w > 0:
        d.rectangle([bx0 + 1, by0 + 1, bx0 + 1 + fill_w, by1 - 1], fill=color)
    d.text((bx1 + 8, y), f"{pct:>5.1f}%", font=font, fill=FG)


def render_frame(width: int, height: int, snapshot, system, storage, connected: bool, fonts) -> Image.Image:
    f_title, f_body, f_small = fonts
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    if not connected:
        msg = "Waiting for MiSTer status server..."
        bbox = d.textbbox((0, 0), msg, font=f_body)
        tw = bbox[2] - bbox[0]
        d.text(((width - tw) // 2, height // 2 - 8), msg, font=f_body, fill=WARN)
        d.text((12, height - 20), time.strftime("%H:%M:%S"), font=f_small, fill=DIM)
        return img

    snapshot = snapshot or {}
    system = system or {}
    storage = storage or {}

    core = snapshot.get("system_name") or snapshot.get("core") or "Menu"
    game = snapshot.get("game") or ""

    # Header
    d.rectangle([0, 0, width, 44], fill=HEADER_BG)
    d.text((12, 5), str(core)[:34], font=f_title, fill=ACCENT)
    if game:
        d.text((12, 27), str(game)[:52], font=f_small, fill=FG)

    y = 56
    draw_stat_bar(d, "CPU", system.get("cpu_usage", 0.0), y, width, f_body)
    y += 28
    draw_stat_bar(d, "MEM", system.get("memory_usage", 0.0), y, width, f_body)
    y += 28

    d.text((12, y), f"UP   {fmt_uptime(system.get('uptime_seconds', 0))}", font=f_body, fill=DIM)
    y += 24

    sd = storage.get("sd_card") or {}
    if sd:
        d.text(
            (12, y),
            f"SD   {sd.get('used_gb', 0):.1f}/{sd.get('total_gb', 0):.1f} GB "
            f"({sd.get('usage_percent', 0):.0f}%)",
            font=f_body, fill=DIM,
        )
        y += 24

    d.text((12, height - 20), time.strftime("%H:%M:%S"), font=f_small, fill=DIM)
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
    ap.add_argument("--once", action="store_true",
                     help="render a single frame and exit (for testing)")
    args = ap.parse_args()

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

    try:
        while True:
            snapshot = fetch_json(args.server, "/status/snapshot")
            system = fetch_json(args.server, "/status/system")
            storage = fetch_json(args.server, "/status/storage")
            connected = snapshot is not None

            frame = render_frame(width, height, snapshot, system, storage, connected, fonts)
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

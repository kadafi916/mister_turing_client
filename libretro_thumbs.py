# SPDX-License-Identifier: GPL-3.0-or-later
"""Client for a self-hosted libretro-artwork-api instance (see the sibling
../libretro-artwork-api project) - a local mirror of libretro-thumbnails
(https://github.com/libretro-thumbnails) served over a tiny HTTP API.

Used as a *fallback* artwork source behind ScreenScraper: ScreenScraper has
richer media (wheel art, marquees, 3D box art, and per-system/per-core art
for the idle "no game loaded" screen) and is tried first when configured,
but its artwork fetching requires a developer key that has to be manually
requested and approved on ScreenScraper's forum - a real bottleneck this
project hit directly (see README.md). libretro-thumbnails needs no
credentials and has no rate limit, so it's a solid fallback when
ScreenScraper is unconfigured, rate-limited, or simply has no data for a
particular game.

This only covers *game* art (box art / snap / title screen), not
system/core art for the idle screen - libretro-thumbnails' per-repo layout
(Named_Boxarts/Named_Snaps/Named_Titles/Named_Logos) has no per-system
"wheel" or "fanart" equivalent the way ScreenScraper's mediaSysteme.php
does, so fetch_system_art() has no libretro-backed counterpart here.

MiSTer's core_raw (e.g. "nes", "tgfx16", "genesis") is passed straight
through as the API's `system` query param - the API's own SYSTEM_MAP uses
this exact core-name vocabulary already (see libretro-artwork-api/app.py),
so no separate translation table is needed on this side.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from turing_lcd.log import logger

# token from config.ini's *_media_order lists -> this API's `type` param.
# libretro-thumbnails only has per-game boxart/snap/title/logo art (no
# wheel/fanart/photo/3D-box equivalents), so tokens without a reasonable
# equivalent are just skipped - whatever real tokens the media_order list
# does contain are tried in the list's own order, first match wins, same
# as ScreenScraper's client.
_TYPE_MAP = {
    "box3d": "boxart",
    "box2d": "boxart",
    "screenshot": "snap",
    "marquee": "logo",
}


class LibretroThumbsClient:
    def __init__(self, base_url: str, cache_dir: str, timeout: float = 8.0):
        self.base_url = (base_url or "").rstrip("/")
        self.cache_dir = cache_dir
        self.timeout = timeout
        os.makedirs(cache_dir, exist_ok=True)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def fetch_game_art(self, core_raw: str, cache_key: str, game_title: str, media_order: str):
        """core_raw: MiSTer's raw core name, passed through as `system`.
        cache_key: crc32 or search_name - kept consistent with the caller's
        own identity key so switching sources doesn't collide on disk.
        game_title: a clean title string (rom_details['search_name'] is
        exactly this - see mister_turing_client.py).
        Returns a local cached file path, or None if unavailable."""
        if not self.configured or not core_raw or not game_title:
            return None

        dest_path = os.path.join(self.cache_dir, f"libretro_{core_raw}_{cache_key}.png")
        if os.path.exists(dest_path):
            return dest_path

        tried_types = set()
        last_reason = None
        for token in media_order.split(","):
            media_type = _TYPE_MAP.get(token.strip().lower())
            if not media_type or media_type in tried_types:
                continue
            tried_types.add(media_type)
            ok, reason = self._download(core_raw, game_title, media_type, dest_path)
            if ok:
                return dest_path
            last_reason = reason

        if tried_types:
            # Visible at the default log level on purpose - a silent
            # no-match here (e.g. the title libretro-thumbnails has on file
            # doesn't fuzzy-match what mister_status_server reported) was
            # previously undiagnosable without guessing; this at least says
            # what was tried and why the last attempt failed.
            logger.info("libretro-thumbnails: no artwork for %s '%s' (tried %s, last: %s)",
                        core_raw, game_title, sorted(tried_types), last_reason or "?")
        return None

    def _download(self, system: str, game: str, media_type: str, dest_path: str):
        """Returns (True, None) on success, (False, reason) otherwise."""
        params = {"system": system, "game": game, "type": media_type}
        url = f"{self.base_url}/artwork?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "mister_turing_client"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            reason = f"HTTP {e.code}"
            if e.code == 404:
                # The API's 404 body is JSON with a real reason ("unmapped
                # system: x" / "no local data for .../restart" / "no match")
                # - surface it instead of discarding it, that's exactly the
                # detail needed to tell "bad system mapping" apart from
                # "title just didn't fuzzy-match".
                try:
                    reason = json.loads(e.read()).get("error", reason)
                except Exception:
                    pass
            else:
                # Anything other than 404 (the expected "try the next
                # thing" outcome) is worth a louder log, same reasoning as
                # ScreenScraper's client's 401/403 handling.
                logger.warning("libretro-artwork-api %s: HTTP %s", url, e.code)
            return False, reason
        except Exception as e:
            logger.debug("libretro-artwork-api %s: %s", url, e)
            return False, str(e)

        tmp_path = dest_path + ".part"
        with open(tmp_path, "wb") as f:
            f.write(body)
        os.replace(tmp_path, dest_path)
        logger.info("Artwork cached (libretro-thumbnails): %s (%s, %d bytes)",
                    os.path.basename(dest_path), media_type, len(body))
        return True, None

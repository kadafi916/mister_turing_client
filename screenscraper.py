# SPDX-License-Identifier: GPL-3.0-or-later
"""ScreenScraper.fr artwork client.

Ported from MiSTer_monitor's ScreenScraper integration
(mister_monitor_CYD28R_ILI9341.ino - GPL-3.0-or-later, same project): the
jeuInfos.php / jeuRecherche.php / mediaJeu.php / mediaSysteme.php call
shapes, the credential model (member ssid/sspassword + a separate developer
devid/devpassword), and the token -> ScreenScraper media-type / region
expansion algorithm (tryMediaTypesForToken / tryMediaTypeWithRegions) are
all deliberately kept identical to the firmware's, since that's proven
behavior against the real API. What's different: this runs on a normal
Python process, not an ESP32 under heap pressure, so there's no bounded
streaming scan - a plain urlopen().read() is fine.

Downloaded art is cached to disk (SD card, effectively unlimited space
here) keyed by CRC or system id, so a game/core already seen is never
re-fetched.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from turing_lcd.log import logger

API_BASE = "https://api.screenscraper.fr/api2"
SOFTNAME = "mister_turing_client"
_ALL_REGIONS = ("wor", "us", "eu", "jp")

# token -> (media_base, has_region_variants, include_generic_after_regions)
# Exact port of tryMediaTypesForToken()'s dispatch table.
_TOKEN_TABLE = {
    "wheel-steel":  ("wheel-steel", True, True),
    "wheel-carbon": ("wheel-carbon", True, True),
    "wheel":        ("wheel", True, True),
    "box3d":        ("box-3D", True, True),
    "box2d":        ("box-2D", True, False),   # no generic variant in the API
    "mix1":         ("mixrbv1", True, True),
    "mix2":         ("mixrbv2", True, True),
    "mix":          ("mixrbv2", True, True),   # alias, per docs
    "fanart":       ("fanart", False, False),
    "screenshot":   ("sstitle", False, False),
    "photo":        ("photo", False, False),
    "illustration": ("illustration", False, False),
}


def _media_variants(token: str, region: str):
    """Yield ScreenScraper &media= strings to try for one config.ini token,
    in the same order the firmware tries them."""
    token = token.strip().lower()

    if token == "marquee":
        # Special-cased in the firmware: generic first, then regions, no
        # second generic attempt.
        yield "marquee"
        for r in (region,) + tuple(x for x in _ALL_REGIONS if x != region):
            yield f"marquee({r})"
        return

    entry = _TOKEN_TABLE.get(token)
    if entry is None:
        logger.debug("Unknown artwork token '%s', skipping", token)
        return
    media_base, has_regions, include_generic = entry

    if not has_regions:
        yield media_base
        return

    # Preferred region first, then the rest in fixed order, then generic.
    yield f"{media_base}({region})"
    for r in _ALL_REGIONS:
        if r != region:
            yield f"{media_base}({r})"
    if include_generic:
        yield media_base


class ScreenScraperClient:
    def __init__(self, config, cache_dir: str, timeout: float = 12.0):
        self.config = config
        self.cache_dir = cache_dir
        self.timeout = timeout
        os.makedirs(cache_dir, exist_ok=True)

    @property
    def configured(self) -> bool:
        return self.config.screenscraper_configured

    # -- low-level HTTP -----------------------------------------------------

    def _auth_params(self) -> dict:
        return {
            "devid": self.config.ss_dev_user,
            "devpassword": self.config.ss_dev_pass,
            "softname": SOFTNAME,
            "ssid": self.config.ss_user,
            "sspassword": self.config.ss_pass,
        }

    def _get_json(self, endpoint: str, params: dict):
        url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": SOFTNAME, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            # Visible at the default log level on purpose: an auth/credential
            # problem here (401/403) would otherwise fail exactly as silently
            # as the RA one did before its server-side print() caught it -
            # this is the equivalent guard on the client side.
            level = logger.warning if e.code in (401, 403) else logger.debug
            level("ScreenScraper %s: HTTP %s", endpoint, e.code)
            return None
        except Exception as e:
            logger.debug("ScreenScraper %s: %s", endpoint, e)
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            # ScreenScraper returns plain-text errors (quota exceeded, bad
            # credentials, etc.) instead of JSON on failure - visible for the
            # same reason as the HTTPError case above.
            logger.warning("ScreenScraper %s: non-JSON response (%s)", endpoint, body[:200])
            return None
        # The documented v2 shape wraps the payload in a top-level "response"
        # key ({"header": ..., "response": {"jeu": ...}}), confirmed against
        # every third-party client we could find - but that's inference, not
        # something read from ScreenScraper's own source, so tolerate an
        # unwrapped body too rather than assume and break silently.
        if isinstance(data, dict) and "response" in data:
            return data["response"]
        return data

    def _download_first_match(self, endpoint: str, base_params: dict, media_order: str, dest_path: str) -> bool:
        last_reason = None
        attempts = 0
        auth_failures = 0
        for token in media_order.split(","):
            for media_type in _media_variants(token, self.config.region):
                attempts += 1
                params = dict(base_params)
                params["media"] = media_type
                url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url, headers={"User-Agent": SOFTNAME})
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        content_type = resp.headers.get("Content-Type", "")
                        body = resp.read()
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        auth_failures += 1
                    last_reason = f"HTTP {e.code}"
                    continue
                except Exception as e:
                    last_reason = str(e)
                    continue
                if not content_type.startswith("image/") or len(body) < 500:
                    # A wrong/rejected credential often comes back as a 200
                    # with an HTML or plain-text error body rather than an
                    # HTTP error status - this is what would catch that.
                    last_reason = f"non-image response (Content-Type: {content_type or 'none'})"
                    continue
                tmp_path = dest_path + ".part"
                with open(tmp_path, "wb") as f:
                    f.write(body)
                os.replace(tmp_path, dest_path)
                logger.info("Artwork cached: %s (%s, %d bytes)", os.path.basename(dest_path), media_type, len(body))
                return True

        if auth_failures:
            logger.warning("Artwork fetch for %s: all %d attempts rejected as unauthorized "
                            "(check ss_dev_user/ss_dev_pass in config.ini)", dest_path, attempts)
        elif attempts:
            logger.info("Artwork fetch for %s: no match in %d attempts (last: %s)",
                        dest_path, attempts, last_reason)
        return False

    # -- game art -------------------------------------------------------------

    def fetch_game_art(self, system_id: str, rom_details: dict, media_order: str):
        """rom_details: the dict from /status/rom/details. Returns a local
        cached file path (str) or None if unavailable/unconfigured."""
        if not self.configured or not system_id:
            return None

        crc32 = (rom_details.get("crc32") or "").strip()
        cache_key = crc32 or (rom_details.get("search_name") or rom_details.get("ss_romnom") or "")
        if not cache_key:
            return None
        dest_path = os.path.join(self.cache_dir, f"game_{system_id}_{cache_key}.jpg")
        if os.path.exists(dest_path):
            return dest_path

        game_id, resolved_system = self._resolve_game_id(system_id, rom_details)
        if not game_id:
            return None

        base_params = self._auth_params()
        base_params.update({
            "crc": "", "md5": "", "sha1": "",
            "systemeid": resolved_system or system_id,
            "jeuid": game_id,
            "outputformat": "jpg",
        })
        if self._download_first_match("mediaJeu.php", base_params, media_order, dest_path):
            return dest_path
        return None

    def _resolve_game_id(self, system_id: str, rom_details: dict):
        """Returns (game_id, resolved_systemeid) via CRC lookup, falling
        back to name search when the CRC route can't work (matches the
        server's own name_search_hint signal)."""
        romnom = rom_details.get("ss_romnom") or rom_details.get("filename") or ""

        if not rom_details.get("name_search_hint") and rom_details.get("crc32"):
            params = self._auth_params()
            params.update({
                "output": "json",
                "systemeid": system_id,
                "romtype": "rom",
                "romnom": romnom,
                "crc": rom_details.get("crc32", ""),
                "romtaille": rom_details.get("size", 0),
                "md5": rom_details.get("md5", ""),
                "sha1": "",
            })
            data = self._get_json("jeuInfos.php", params)
            jeu = (data or {}).get("jeu")
            if jeu and jeu.get("id"):
                sys_id = (jeu.get("systeme") or {}).get("id", system_id)
                return jeu["id"], sys_id

        if not romnom:
            return None, None

        params = self._auth_params()
        params.update({"output": "json", "systemeid": system_id, "recherche": romnom})
        data = self._get_json("jeuRecherche.php", params)
        candidates = (data or {}).get("jeux") or []
        if candidates:
            jeu = candidates[0]
            sys_id = (jeu.get("systeme") or {}).get("id", system_id)
            return jeu.get("id"), sys_id
        return None, None

    # -- system/core art --------------------------------------------------

    def fetch_system_art(self, system_id: str, cache_key: str, media_order: str):
        if not self.configured or not system_id:
            return None
        dest_path = os.path.join(self.cache_dir, f"system_{cache_key}.jpg")
        if os.path.exists(dest_path):
            return dest_path

        base_params = self._auth_params()
        base_params.update({"crc": "", "md5": "", "sha1": "", "systemeid": system_id})
        if self._download_first_match("mediaSysteme.php", base_params, media_order, dest_path):
            return dest_path
        return None

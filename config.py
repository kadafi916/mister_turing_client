# SPDX-License-Identifier: GPL-3.0-or-later
"""config.ini loader for mister_turing_client.

Mirrors the [screenscraper] section format documented in MiSTer_monitor's
docs/configuration.md, so anyone already familiar with that project's
config.ini recognizes this one. RetroAchievements needs no config here -
it's entirely server-side (ra_credentials.ini on the MiSTer itself).
"""

import configparser
import os

DEFAULT_CORE_MEDIA_ORDER = "wheel-steel,wheel-carbon,wheel,photo,illustration,box3d,box2d,marquee,fanart,screenshot"
DEFAULT_ARCADE_SUBSYSTEM_MEDIA_ORDER = "wheel-steel,wheel-carbon,wheel"
DEFAULT_ARCADE_MEDIA_ORDER = "fanart,marquee,wheel-carbon,wheel-steel,wheel,box3d,box2d,screenshot"
DEFAULT_GAME_MEDIA_ORDER = "box3d,box2d,wheel-carbon,wheel-steel,wheel,fanart,marquee,screenshot"


class Config:
    def __init__(self, path: str):
        self.path = path
        parser = configparser.ConfigParser()
        if os.path.exists(path):
            parser.read(path)

        ss = parser["screenscraper"] if parser.has_section("screenscraper") else {}
        self.ss_user = ss.get("ss_user", "").strip()
        self.ss_pass = ss.get("ss_pass", "").strip()
        self.ss_dev_user = ss.get("ss_dev_user", "").strip()
        self.ss_dev_pass = ss.get("ss_dev_pass", "").strip()
        self.region = ss.get("region", "us").strip() or "us"

        libretro = parser["libretro"] if parser.has_section("libretro") else {}
        self.libretro_base_url = libretro.get("base_url", "").strip()

        images = parser["images"] if parser.has_section("images") else {}
        self.core_media_order = images.get("core_media_order", DEFAULT_CORE_MEDIA_ORDER)
        self.arcade_subsystem_media_order = images.get(
            "arcade_subsystem_media_order", DEFAULT_ARCADE_SUBSYSTEM_MEDIA_ORDER)
        self.arcade_media_order = images.get("arcade_media_order", DEFAULT_ARCADE_MEDIA_ORDER)
        self.game_media_order = images.get("game_media_order", DEFAULT_GAME_MEDIA_ORDER)

        # Page rotation. Raw strings, deliberately unvalidated/undefaulted
        # here (empty = "not set") - mister_turing_client.py's ALL_PAGES/
        # DEFAULT_PAGE_SECONDS are the actual fallbacks and the only place
        # page names are validated, to avoid this module needing to import
        # from (and risk a circular import with) the main script.
        pages = parser["pages"] if parser.has_section("pages") else {}
        self.pages = pages.get("pages", "").strip()
        self.page_seconds = pages.get("page_seconds", "").strip()

        # Optional: a different rotation while an RA-adapted core is
        # actively running (is_ra_core_active()) - e.g. cover art only
        # normally, full rotation back the moment you're playing something
        # RA-tracked. Empty/absent means "no RA-specific override", not
        # "empty rotation" - see main()'s fallback to [pages] above.
        pages_ra = parser["pages_ra"] if parser.has_section("pages_ra") else {}
        self.pages_ra = pages_ra.get("pages", "").strip()
        self.page_seconds_ra = pages_ra.get("page_seconds", "").strip()

    @property
    def screenscraper_configured(self) -> bool:
        return bool(self.ss_user and self.ss_pass and self.ss_dev_user and self.ss_dev_pass)

# SPDX-License-Identifier: GPL-3.0-or-later
"""RetroAchievements client - thin wrapper around mister_status_server.py's
already-complete RA implementation.

Unlike ScreenScraper, there is no API work to port here: the server
(ra_status.py / ra_hash.py) does the hashing, talks to the RA Web API, and
tracks unlock events. This module just polls its JSON endpoints
(/status/retroachievements and /status/retroachievements/achievements),
gives the fields sane attribute access, and detects new unlocks by
watching event_counter for a popup to trigger on.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RAStatus:
    enabled: bool = False
    status: str = "unknown"          # "not_configured" | "no_credentials" | "ok" | "module_unavailable" | ...
    supported: bool = False
    game_matched: bool = False
    game_id: int = 0
    game_title: str = ""
    total: int = 0
    unlocked: int = 0
    unlocked_hardcore: int = 0
    points_earned: int = 0
    points_total: int = 0
    points_hardcore: int = 0
    event_counter: int = 0
    last_unlock_title: str = ""
    last_unlock_points: int = 0
    last_unlock_hardcore: bool = False
    last_unlock_description: str = ""
    unlocks_tracked: bool = False    # True once the odelot fork is detected

    @classmethod
    def from_json(cls, data: dict) -> "RAStatus":
        if not data:
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            status=data.get("status", "unknown"),
            supported=data.get("supported", False),
            game_matched=data.get("game_matched", False),
            game_id=data.get("game_id", 0),
            game_title=data.get("game_title", ""),
            total=data.get("total", 0),
            unlocked=data.get("unlocked", 0),
            unlocked_hardcore=data.get("unlocked_hardcore", 0),
            points_earned=data.get("points_earned", 0),
            points_total=data.get("points_total", 0),
            points_hardcore=data.get("points_hardcore", 0),
            event_counter=data.get("event_counter", 0),
            last_unlock_title=data.get("last_unlock_title", ""),
            last_unlock_points=data.get("last_unlock_points", 0),
            last_unlock_hardcore=data.get("last_unlock_hardcore", False),
            last_unlock_description=data.get("last_unlock_description", ""),
            unlocks_tracked=data.get("unlocks_tracked", False),
        )

    @property
    def progress_pct(self) -> float:
        if not self.total:
            return 0.0
        return 100.0 * self.unlocked / self.total


@dataclass
class Achievement:
    title: str = ""
    description: str = ""
    points: int = 0
    unlocked: bool = False
    hardcore: bool = False
    badge_url: str = ""

    @classmethod
    def from_json(cls, d: dict) -> "Achievement":
        return cls(
            title=d.get("title") or d.get("Title", ""),
            description=d.get("description") or d.get("Description", ""),
            points=d.get("points") or d.get("Points", 0),
            unlocked=bool(d.get("unlocked") or d.get("Unlocked")),
            hardcore=bool(d.get("hardcore") or d.get("HardcoreUnlocked")),
            badge_url=d.get("badge_url") or d.get("BadgeURL", ""),
        )


class UnlockTracker:
    """Detects a new unlock by watching event_counter, so the caller can
    show a popup exactly once per event rather than every poll cycle."""

    def __init__(self):
        self._last_seen: Optional[int] = None

    def check(self, status: RAStatus) -> bool:
        """Returns True the first time a new event_counter value is seen
        (and False on the very first call, so we don't "unlock" everything
        already earned before this client started)."""
        if self._last_seen is None:
            self._last_seen = status.event_counter
            return False
        if status.event_counter != self._last_seen:
            self._last_seen = status.event_counter
            return status.last_unlock_title != ""
        return False


def fetch_status(fetch_json, base_url: str) -> RAStatus:
    data = fetch_json(base_url, "/status/retroachievements")
    return RAStatus.from_json(data)


def fetch_achievements_page(fetch_json, base_url: str, page: int = 0):
    """Returns (achievements: list[Achievement], page, pages)."""
    data = fetch_json(base_url, f"/status/retroachievements/achievements?page={page}")
    if not data:
        return [], 0, 0
    items = data.get("achievements") or []
    return [Achievement.from_json(a) for a in items], data.get("page", 0), data.get("pages", 0)

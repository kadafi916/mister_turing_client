# SPDX-License-Identifier: GPL-3.0-or-later
"""MiSTer core name -> ScreenScraper numeric systemeid.

Ported from MiSTer_monitor's mapCoreToScreenScraperId() (mister_monitor_CYD28R_ILI9341.ino) -
same GPL-3.0-or-later project, since this table is what the firmware itself
uses. Only the resolution *table* is ported; the extension-based back-compat
disambiguation (ssSystemForRom() - e.g. a 2600 cartridge loaded on the 7800
core, an SMS-flavoured .gg on the Game Gear core) is not implemented yet -
those cores fall back to their own system id, which is correct in the
overwhelming majority of cases and only wrong for the small set of
cross-library edge cases that function handles. Worth porting later if it
turns out to matter in practice.
"""

# Friendly system_name (as reported by /status/snapshot's "system_name"),
# case-sensitive exact match first.
_BY_FRIENDLY_NAME = {
    # Nintendo
    "Nintendo Entertainment System": "3", "Nintendo NES/Famicom": "3",
    "Super Nintendo Entertainment System": "4", "Super Nintendo": "4",
    "Super Nintendo/Super Famicom": "4",
    "Nintendo 64": "14",
    "Nintendo Game Boy": "9", "Game Boy": "9",
    "Nintendo Game Boy Color": "10", "Game Boy Color": "10",
    "Nintendo Game Boy Advance": "12", "Game Boy Advance": "12",
    "Nintendo Game Boy Advance 2P": "12",
    "Famicom Disk System": "106", "Family Computer Disk System": "106",
    "Nintendo Super Game Boy": "127", "Super Game Boy": "127",
    "Nintendo Game & Watch": "52", "Game & Watch": "52",
    "Nintendo Virtual Boy": "11",

    # Sega
    "Sega Genesis/Mega Drive": "1", "Megadrive": "1",
    "Megadrive 32X": "19", "Sega Genesis/Megadrive 32X": "19",
    "Sega Master System": "2", "Master System": "2",
    "Sega Game Gear": "21", "Game Gear": "21",
    "Sega Saturn": "22", "Saturn": "22",
    "Sega Mega-CD": "20", "Sega CD/Mega CD": "20", "MegaCD": "20",
    "Sega SG-1000": "109", "SG-1000": "109",

    # Sony
    "PlayStation": "57", "Sony PlayStation": "57",

    # PC Engine / TurboGrafx
    "TurboGrafx-16/PC Engine": "31", "PC Engine": "31",
    "PC Engine CD-Rom": "114", "TurboGrafx-16/PC Engine CD-Rom": "114",
    "PC Engine SuperGrafx": "105", "SuperGrafx": "105",

    # Neo-Geo
    "Neo-Geo": "142",
    "Neo-Geo CD": "70",
    "Neo Geo Pocket": "25", "Neo-Geo Pocket": "25",
    "Neo Geo Pocket Color": "82", "Neo-Geo Pocket Color": "82",

    # Arcade
    "Arcade": "75", "mame": "75", "MAME": "75",
    "Multiple Arcade Machine Emulator": "75",

    # Atari
    "Atari 2600": "26", "Atari 5200": "40", "Atari 7800": "41",
    "Atari Lynx": "28", "Atari Lynx (2P)": "28",
    "Atari Jaguar": "27", "Jaguar": "27",
    "Atari ST/STE": "42", "Atari ST": "42",
    "Atari 8bit": "43",

    # Commodore / Amiga
    "Commodore Amiga": "64", "Amiga CD32": "130",
    "Commodore 64": "66", "Commodore 128": "66",
    "Vic-20": "73", "Commodore VIC-20": "73", "Commodore Vic-20": "73",
    "PET": "240", "Commodore PET": "240",
    "C16": "99",

    # PC / DOS
    "PC Dos": "135",

    # British micros
    "ZX Spectrum": "76", "ZX81": "77",
    "Amstrad CPC": "65", "Amstrad GX4000": "65", "CPC": "65",
    "Acorn Electron": "85", "Electron": "85",
    "Acorn Atom": "36", "Atom": "36",
    "Acorn Archimedes": "84", "Archimedes": "84",
    "BBC Micro": "37",
    "MGT SAM Coupé": "213", "SAM Coupé": "213",

    # MSX
    "MSX": "113", "MSX1": "113",
    "MSX2 Computer": "116", "MSX2": "116",
    "MSX2+ Computer": "116", "MSX2Plus": "116",

    # Other
    "BK": "93", "Elektronika BK0011M": "93",
    "Tomy Tutor / Pyuta / Pyuta Jr.": "317",
    "Jupiter Ace": "126", "Tamagotchi": "293",
    "EG2000 Colour Genie": "92", "Camputers Lynx": "88",
    "NEC PC-8801": "221", "PC-9801": "120", "FM-7": "97",
    "Spectravideo SVI-328": "218", "TI-99/4A": "205",
    "Sharp X68000": "79",

    # Apple
    "Apple II": "86",
    "Apple IIgs": "217", "Apple 2GS": "217",
    "Apple Macintosh Plus": "146", "Mac OS": "146",
    "Apple Macintosh LC": "146",

    # Misc consoles / handhelds
    "Vectrex": "102", "Intellivision": "115", "Colecovision": "48",
    "WonderSwan": "45",
    "WonderSwan Color": "46", "WonderSwanColor": "46",
    "Oric 1 / Atmos": "131",
    "Videopac G7000": "104", "Videopac G7000/Odyssey 2": "104",
    "CreatiVision": "241", "Channel F": "80", "Astrocade": "44",
    "Arcadia 2001": "94", "Adventure Vision": "78", "Adam": "89",
    "PV-1000": "74", "Casio PV-1000": "74",
    "CD-i": "133", "Philips CD-i": "133", "Phillips CD-i": "133",
    "3DO": "29", "Panasonic 3DO": "29",
    "Super Cassette Vision": "67", "SCV": "67",
    "Gamate": "266", "Bit Corporation Gamate": "266",
    "Mega Duck": "90", "Pocket Challenge V2": "237",
    "Pokemon Mini": "211", "Watara Supervision": "207",
    "VC 4000": "281", "Interton VC 4000": "281",

    # TRS-80 / Tandy
    "TRS-80 Color Computer": "144",
    "TRS-80 Color Computer 2": "144",
    "TRS-80 Color Computer 3": "144",

    "Menu": None,
}

# Fallback table: lowercased raw CORENAME (as reported by /status/snapshot's
# "core_raw") or alternate spellings.
_BY_LOWER_CORENAME = {
    "nes": "3", "nintendo": "3",
    "snes": "4", "supernintendo": "4",
    "n64": "14", "nintendo64": "14",
    "gameboy": "9", "gb": "9",
    "gbc": "10", "gameboycolor": "10",
    "gba": "12", "gameboyadvance": "12",
    "fds": "106", "sgb": "127",
    "genesis": "1", "megadrive": "1", "md": "1",
    "s32x": "19",
    "mastersystem": "2", "sms": "2",
    "gg": "21", "saturn": "22",
    "megacd": "20", "segacd": "20",
    "psx": "57", "playstation": "57",
    "tgfx16": "31", "pcengine": "31", "turbografx16": "31",
    "neogeo": "142", "neo-geo": "142",
    "arcade": "75", "mame": "75", "multiple arcade machine emulator": "75",
    "atari2600": "26", "atari5200": "40", "atari7800": "41",
    "atarilynx": "28", "atarilynx2p": "28",
    "atarist": "42",
    "amiga": "64", "minimig": "64", "amiga500": "64", "amiga500hd": "64",
    "amiga600hd": "64", "commodore amiga": "64",
    "amigacd32": "130",
    "c64": "66", "commodore64": "66", "c128": "66",
    "ao486": "135", "pc dos": "135", "pcxt": "135",
    "atari800": "43", "coco3": "144", "colecovision": "48",
    "gameboy2p": "10", "virtualboy": "11", "odyssey2": "104",
    "svi328": "218",
    "apple-iigs": "217", "appleiigs": "217",
    "apple-ii": "86", "apple2": "86",
    "maclc": "146", "macplus": "146",
    "amstrad": "65", "cpc": "65",
    "sam": "213", "samcoupe": "213",
    "x68000": "79",
    "wonderswan": "45", "wonderswancolor": "46",
    "vectrex": "102", "coleco": "48", "intellivision": "115",
    "3do": "29", "supergrafx": "105",
    "ngp": "25", "ngpc": "82", "gba2p": "12", "scv": "67", "jaguar": "27",
    "bk0011m": "93", "bk": "93",
    "tomytutor": "317", "jupiter": "126", "tamagotchi": "293",
    "menu": None, "main": None,
}


def get_system_id(system_name: str = "", core_raw: str = "") -> str:
    """Resolve a ScreenScraper systemeid for a MiSTer core, or "" if unknown.

    Mirrors getScreenScraperSystemId()'s resolution order: the raw CORENAME
    (lowercased) is tried first when available, then the friendly
    system_name exactly, then the friendly name lowercased.
    """
    if core_raw:
        sid = _BY_LOWER_CORENAME.get(core_raw.strip().lower())
        if sid is not None:
            return sid

    if system_name:
        sid = _BY_FRIENDLY_NAME.get(system_name)
        if sid is not None:
            return sid
        sid = _BY_LOWER_CORENAME.get(system_name.strip().lower())
        if sid is not None:
            return sid

    return ""

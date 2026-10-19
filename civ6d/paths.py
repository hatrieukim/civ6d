"""Where the game and its user data live.

Defaults are the macOS Steam/Aspyr layout; override with environment variables:
  CIV6_APP       the game bundle (…/steamapps/common/Sid Meier's Civilization VI/Civ6.app)
  CIV6_USER_DIR  the user data folder (~/Library/Application Support/Sid Meier's Civilization VI)
"""

from __future__ import annotations

import os
from pathlib import Path

_APP_SUPPORT = Path.home() / "Library/Application Support"

GAME_APP = Path(os.environ.get(
    "CIV6_APP", _APP_SUPPORT / "Steam/steamapps/common/Sid Meier's Civilization VI/Civ6.app"))
USER_DIR = Path(os.environ.get("CIV6_USER_DIR", _APP_SUPPORT / "Sid Meier's Civilization VI"))

ASSETS = GAME_APP / "Contents/Assets"  # the game's own UI Lua: the reference for every API
SAVE_DIR = USER_DIR / "Sid Meier's Civilization VI/Saves/Single"
CONFIG_DIR = USER_DIR / "Firaxis Games/Sid Meier's Civilization VI"  # AppOptions.txt, Cache/
CACHE_DIR = CONFIG_DIR / "Cache"

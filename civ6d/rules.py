"""Read-only access to the game's rules database.

The game writes Cache/DebugGameplay.sqlite every time it configures a game
("Saving a debug version of the gameplay database" in Modding.log), so it holds
the ruleset of the loaded game: base + enabled DLC/mods. ~430 tables (Units,
Buildings, Districts, Technologies, Civics, Improvements, Modifiers, ...).
Lookups here cost no tuner call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import CACHE_DIR

DB: Path = CACHE_DIR / "DebugGameplay.sqlite"
LOC_DB = DB.with_name("DebugLocalization.sqlite")


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def query(sql: str, limit: int = 200) -> dict:
    s = sql.strip().rstrip(";")
    head = s.split(None, 1)[0].lower() if s else ""
    if head not in ("select", "with", "pragma") or ";" in s:
        raise ValueError("read-only: one SELECT/WITH/PRAGMA statement only")
    with _connect(DB) as con:
        cur = con.execute(s)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
    return {"columns": cols, "rows": [list(r) for r in rows[:limit]], "truncated": len(rows) > limit,
            "db_modified": DB.stat().st_mtime}


def tables(like: str = "") -> list[str]:
    with _connect(DB) as con:
        return [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' and name like ? order by name", (f"%{like}%",))]


def describe(table: str) -> list[dict]:
    if not table.replace("_", "").isalnum():
        raise ValueError("bad table name")
    with _connect(DB) as con:
        return [{"name": r["name"], "type": r["type"], "notnull": bool(r["notnull"]), "pk": bool(r["pk"])}
                for r in con.execute(f"pragma table_info({table})")]


def text(tag: str, language: str = "en_US") -> str | None:
    """LOC_* tag -> English text (from DebugLocalization.sqlite)."""
    if not LOC_DB.exists():
        return None
    with _connect(LOC_DB) as con:
        r = con.execute("select Text from LocalizedText where Tag=? and Language=? limit 1", (tag, language)).fetchone()
    return r[0] if r else None

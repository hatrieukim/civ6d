"""Baseline: latency of raw tuner calls vs civ6-mcp read tools, plus output size.

Run: uv run --project civ6-mcp python tools/bench_baseline.py
Read-only: does not change game state.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "civ6-mcp" / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from civ_mcp import narrate as nr  # noqa: E402
from civ_mcp.connection import GameConnection  # noqa: E402
from civ_mcp.game_state import GameState  # noqa: E402
from tuner import Tuner  # noqa: E402


def stats(xs: list[float]) -> dict:
    return {"n": len(xs), "median_ms": round(statistics.median(xs), 1), "max_ms": round(max(xs), 1)}


async def timed(fn):
    t0 = time.perf_counter()
    r = await fn()
    return r, (time.perf_counter() - t0) * 1000


async def main() -> None:
    out: dict = {}

    conn = GameConnection()
    for _ in range(30):
        try:
            await conn.connect()
            break
        except ConnectionError:
            await asyncio.sleep(0.5)
    gs = GameState(conn)

    # 1. Raw tuner round-trip on the same socket (sentinel-terminated, no fixed drains)
    t = Tuner.from_streams(conn._reader, conn._writer, conn.lua_states)
    for state in ("GameCore_Tuner", "InGame"):
        xs = []
        for _ in range(20):
            _, ms = await timed(lambda: t.run(state, "print(Game.GetCurrentGameTurn())"))
            xs.append(ms)
        out[f"raw_{state}"] = stats(xs)

    # 2. civ6-mcp connection (fixed 0.1s + 0.2s drains) and high-level read tools
    xs = []
    for _ in range(10):
        _, ms = await timed(lambda: conn.execute_read('print(Game.GetCurrentGameTurn()); print("---END---")'))
        xs.append(ms)
    out["civ6mcp_execute_read_trivial"] = stats(xs)

    async def units_tool():
        units = await gs.get_units()
        threats = await gs.get_threat_scan()
        trade = await gs.get_trade_routes()
        return nr.narrate_units(units, threats, trade)

    async def cities_tool():
        cities, dist = await gs.get_cities()
        return nr.narrate_cities(cities, dist)

    async def map_tool():
        ov = await gs.get_game_overview()
        units = await gs.get_units()
        u = next((u for u in units if u.x >= 0), None)
        return nr.narrate_map(await gs.get_map_area(u.x, u.y, 4)) if u else ""

    tools = {
        "get_game_overview": lambda: _n(gs.get_game_overview, nr.narrate_overview),
        "get_units": units_tool,
        "get_cities": cities_tool,
        "get_tech_civics": lambda: _n(gs.get_tech_civics, nr.narrate_tech_civics),
        "get_diplomacy": lambda: _n(gs.get_diplomacy, nr.narrate_diplomacy),
        "get_strategic_map": lambda: _n(gs.get_strategic_map, nr.narrate_strategic_map),
        "get_victory_progress": lambda: _n(gs.get_victory_progress, nr.narrate_victory_progress),
        "get_notifications": lambda: _n(gs.get_notifications, nr.narrate_notifications),
        "get_map_area_r4(+overview+units)": map_tool,
    }
    samples = {}
    for name, fn in tools.items():
        try:
            text, ms = await timed(fn)
            out[name] = {"ms": round(ms), "chars": len(text), "approx_tokens": len(text) // 4}
            samples[name] = text[:1500]
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": repr(e)[:300]}
    await conn.disconnect()

    print(json.dumps(out, indent=1, ensure_ascii=False))
    (ROOT / "bench").mkdir(exist_ok=True)
    (ROOT / "bench" / "baseline.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    (ROOT / "bench" / "baseline_samples.txt").write_text(
        "\n\n".join(f"===== {k}\n{v}" for k, v in samples.items())
    )


async def _n(get, narrate):
    return narrate(await get())


if __name__ == "__main__":
    asyncio.run(main())

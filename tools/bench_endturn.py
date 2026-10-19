"""Time civ6-mcp's end_turn (snapshot + poll + diff + 0_MCP save) for one turn."""
import asyncio, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "civ6-mcp" / "src"))
from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState
from civ_mcp.end_turn import execute_end_turn

async def main():
    conn = GameConnection()
    for _ in range(30):
        try:
            await conn.connect(); break
        except ConnectionError:
            await asyncio.sleep(0.5)
    gs = GameState(conn)
    t0 = time.perf_counter(); r = await gs.skip_remaining_units(); t1 = time.perf_counter()
    print(f"skip_remaining_units {1000*(t1-t0):.0f} ms: {r[:120]}")
    t0 = time.perf_counter(); r = await execute_end_turn(gs); t1 = time.perf_counter()
    print(f"execute_end_turn {t1-t0:.2f} s, {len(r)} chars (~{len(r)//4} tokens)")
    print(r[:1500])
    await conn.disconnect()
asyncio.run(main())

"""From the main menu: load a save, flip Automation autostart as soon as the InGame
Lua state exists, and check the load screen was passed without a click.

Run: uv run --project civ6-mcp python tools/probe_autoload.py [save_name]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "civ6-mcp" / "src"))
from civ_mcp import tuner_client as tc  # noqa: E402
from tuner import Tuner  # noqa: E402

QUERY = """
local E = AppUIEnumsLibrary; ExposedMembers.P_saves = nil
local function h(fl, qid) ExposedMembers.P_saves = fl; UI.CloseFileListQuery(qid); LuaEvents.FileListQueryResults.Remove(h) end
LuaEvents.FileListQueryResults.Add(h)
UI.QuerySaveGameList(E.SaveLocations.LOCAL_STORAGE, E.SaveTypes.SINGLE_PLAYER, E.SaveLocationOptions.NORMAL + E.SaveLocationOptions.LOAD_METADATA)
"""


async def refresh_states(t: Tuner) -> None:
    await tc.send_message(t.w, tc.TAG_HANDSHAKE, "LSQ:")
    while True:
        msg = await tc.recv_message_timeout(t.r, timeout=3)
        if msg is None:
            return
        parts = [s.strip() for s in msg.payload.split("\x00") if s.strip()]
        if len(parts) >= 2 and parts[0].lstrip("-").isdigit():
            t.states = {}
            t.adopt_states(parts)
            return


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "0A_GROUND_CONTROL"
    t0 = time.monotonic()
    async with Tuner() as t:
        print("states at connect:", list(t.states)[:5])
        await t.run("Main State", QUERY)
        for _ in range(50):
            saves = await t.run("Main State", "for i,s in ipairs(ExposedMembers.P_saves or {}) do print(i .. '|' .. s.Name) end")
            if saves:
                break
            await asyncio.sleep(0.1)
        idx = next(l.split("|")[0] for l in saves if l.split("|")[1].startswith(name))
        await t.run("Main State", f"Network.LoadGame(ExposedMembers.P_saves[{idx}], LuaNetworkEnumsLibrary.ServerType.SERVER_TYPE_NONE)")
        print(f"LoadGame sent at {time.monotonic() - t0:.1f}s")
    # Keep NO connection open during the load: a held socket + repeated LSQ killed
    # the tuner (4318 closed, only 4319 left). Poll with fresh short connections.
    # Stay off the socket until the reloaded tuner listens on 4318 again.
    import subprocess
    while time.monotonic() - t0 < 300:
        await asyncio.sleep(2)
        up = subprocess.run(["lsof", "-nP", "-iTCP:4318", "-sTCP:LISTEN"], capture_output=True).returncode == 0
        print(f"{time.monotonic() - t0:.1f}s 4318 listening={up}", flush=True)
        if up and time.monotonic() - t0 > 15:
            break
    ctx = None
    while time.monotonic() - t0 < 360:
        await asyncio.sleep(2)
        try:
            async with Tuner() as t:
                if "InGame" not in t.states:
                    continue
                r = await t.run("InGame", AUTO)
                print(f"{time.monotonic() - t0:.1f}s InGame: {r}")
                ctx = r[-1] if r else None
                if ctx == "World":
                    break
        except Exception as e:  # noqa: BLE001
            print(f"{time.monotonic() - t0:.1f}s poll error: {repr(e)[:100]}")
    async with Tuner() as t:
        print(await t.run("GameCore_Tuner", "print('turn ' .. Game.GetCurrentGameTurn())"))


AUTO = """
if not Automation.IsAutoStartEnabled() then Automation.SetAutoStartEnabled(true) print('autostart set') end
local function ctxname() local c = Input.GetActiveContext(); for k, v in pairs(InputContext) do if v == c then return k end end end
local c = ctxname()
if c == 'Ready' then Events.LoadGameViewStateDone() print('refired LoadGameViewStateDone') c = ctxname() end
print(c)
"""


if __name__ == "__main__":
    asyncio.run(main())

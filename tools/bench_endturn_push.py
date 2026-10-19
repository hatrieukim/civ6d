"""End one turn the fast way: fortify/skip idle units in one chunk, request end turn,
wait for the pushed LocalPlayerTurnBegin (handlers from probe_events.py must be installed)."""
import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tuner import Tuner

SKIP = """
local p = Players[Game.GetLocalPlayer()]; local n = 0
for _, u in p:GetUnits():Members() do
  if u:GetMovesRemaining() > 0 then
    if UnitManager.CanStartOperation(u, UnitOperationTypes.FORTIFY, nil, nil) then
      UnitManager.RequestOperation(u, UnitOperationTypes.FORTIFY, {})
    else UnitManager.RequestCommand(u, UnitCommandTypes.SKIP_TURN, {}) end
    n = n + 1
  end
end
print("ordered " .. n .. " installed=" .. tostring(ExposedMembers.AG ~= nil and ExposedMembers.AG.handlers ~= nil))
"""

async def main():
    async with Tuner() as t:
        t0 = time.monotonic()
        print(await t.run("InGame", SKIP), f"{(time.monotonic()-t0)*1000:.0f} ms")
        await asyncio.sleep(0.05)
        print(await t.run("InGame", "print('turn=' .. Game.GetCurrentGameTurn() .. ' canEnd=' .. tostring(UI.CanEndTurn()) .. ' blocking=' .. tostring(NotificationManager.GetFirstEndTurnBlocking(Game.GetLocalPlayer())))"))
        t.pushed.clear()
        t0 = time.monotonic()
        await t.run("InGame", "UI.RequestAction(ActionTypes.ACTION_ENDTURN)")
        ev = await t.wait_push("LocalPlayerTurnBegin", 180)
        print(f"end turn -> LocalPlayerTurnBegin pushed after {ev and round(ev[0]-t0, 2)} s, {len(t.pushed)} pushes")
        print(await t.run("GameCore_Tuner", "print('turn now ' .. Game.GetCurrentGameTurn())"))
asyncio.run(main())

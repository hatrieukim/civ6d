"""Probe 4 questions on a live game over ONE tuner connection:

1. Can tuner-injected Lua register Events handlers that keep firing?
2. Do print()s from those handlers get pushed to us without a request?
3. Can several orders go in one Lua chunk (batch)?
4. How long / how many game-core publishes until an order's effect is readable?

Changes game state (moves warrior, founds city, ends one turn) — use a test save.
Run: uv run --project civ6-mcp python tools/probe_events.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from tuner import Tuner  # noqa: E402

EVENTS = [
    "LocalPlayerTurnBegin", "LocalPlayerTurnEnd", "TurnBegin", "TurnEnd",
    "PlayerTurnActivated", "PlayerTurnDeactivated", "UnitMoved", "UnitMoveComplete",
    "CityAddedToMap", "UnitRemovedFromMap", "NotificationAdded", "ResearchChanged",
    "CityProductionChanged",
]

INSTALL = """
ExposedMembers.AG = ExposedMembers.AG or {}
local AG = ExposedMembers.AG
if AG.handlers then for n,f in pairs(AG.handlers) do pcall(function() Events[n].Remove(f) end) end end
AG.handlers, AG.q, AG.pub = {}, {}, 0
local function add(n)
  if not Events[n] then print("missing "..n) return end
  local f = function(...)
    local a = {...}; local s = n
    for i = 1, 4 do if a[i] ~= nil then s = s .. "|" .. tostring(a[i]) end end
    AG.q[#AG.q+1] = s
    print("@@EV|" .. s .. "|pub=" .. AG.pub)
  end
  Events[n].Add(f); AG.handlers[n] = f
end
for _, n in ipairs(%s) do add(n) end
local fc = function() AG.pub = AG.pub + 1 end
if Events.GameCoreEventPublishComplete then
  Events.GameCoreEventPublishComplete.Add(fc); AG.handlers.GameCoreEventPublishComplete = fc
else print("missing GameCoreEventPublishComplete") end
print("installed")
""" % ("{" + ",".join(f'"{e}"' for e in EVENTS) + "}")

UNITS = """
local p = Players[Game.GetLocalPlayer()]
for _, u in p:GetUnits():Members() do
  print(u:GetID() .. "," .. GameInfo.Units[u:GetType()].UnitType .. "," .. u:GetX() .. "," .. u:GetY() .. "," .. u:GetMovesRemaining())
end
"""


def ms(a: float, b: float) -> float:
    return round((b - a) * 1000, 1)


async def pub(t: Tuner) -> int:
    return int((await t.run("InGame", "print(ExposedMembers.AG.pub)"))[0])


async def main() -> None:
    rep: dict = {}
    async with Tuner() as t:
        rep["install"] = await t.run("InGame", INSTALL)

        # Q1/Q2: idle listen — anything pushed without a request?
        idle = await t.listen(2.0)
        rep["idle_2s_msgs"] = len(idle)
        rep["idle_2s_sample"] = [m for _, m in idle[:5]]
        rep["pub_count_after_idle"] = await pub(t)

        units = [l.split(",") for l in await t.run("InGame", UNITS)]
        rep["units"] = units
        warrior = next(u for u in units if u[1] == "UNIT_WARRIOR")
        settler = next(u for u in units if u[1] == "UNIT_SETTLER")
        wid = int(warrior[0])

        # Q4: move warrior to first legal neighbour; time until position readable
        move = f"""
local p = Players[Game.GetLocalPlayer()]; local u = p:GetUnits():FindID({wid})
for d = 0, 5 do
  local pl = Map.GetAdjacentPlot(u:GetX(), u:GetY(), d)
  if pl then
    local prm = {{}}; prm[UnitOperationTypes.PARAM_X] = pl:GetX(); prm[UnitOperationTypes.PARAM_Y] = pl:GetY()
    if UnitManager.CanStartOperation(u, UnitOperationTypes.MOVE_TO, nil, prm) then
      UnitManager.RequestOperation(u, UnitOperationTypes.MOVE_TO, prm)
      print("target," .. pl:GetX() .. "," .. pl:GetY())
      print("same_chunk_pos," .. u:GetX() .. "," .. u:GetY() .. ",pub=" .. ExposedMembers.AG.pub)
      break
    end
  end
end
"""
        pub0 = await pub(t)
        t0 = time.monotonic()
        res = await t.run("InGame", move)
        rep["move_request"] = res
        target = next(l for l in res if l.startswith("target")).split(",")[1:]
        polls, t_seen, t_seen_ingame = 0, None, None
        while time.monotonic() - t0 < 5 and (t_seen is None or t_seen_ingame is None):
            polls += 1
            if t_seen is None:
                pos = (await t.run("GameCore_Tuner", f"local u=UnitManager.GetUnit(Game.GetLocalPlayer(),{wid}); print(u:GetX()..','..u:GetY())"))[0]
                if pos.split(",") == target:
                    t_seen = time.monotonic()
            if t_seen_ingame is None:
                pos = (await t.run("InGame", f"local u=Players[Game.GetLocalPlayer()]:GetUnits():FindID({wid}); print(u:GetX()..','..u:GetY())"))[0]
                if pos.split(",") == target:
                    t_seen_ingame = time.monotonic()
        ev = await t.wait_push("UnitMoveComplete", 3)
        rep["move"] = {
            "gamecore_pos_updated_ms": t_seen and ms(t0, t_seen),
            "ingame_pos_updated_ms": t_seen_ingame and ms(t0, t_seen_ingame),
            "polls": polls,
            "pub_delta": (await pub(t)) - pub0,
            "push_UnitMoveComplete_ms": ev and ms(t0, ev[0]),
            "pushes": [(ms(t0, a), b) for a, b in t.pushed if a >= t0][:10],
        }

        # Q3: batch — found city + fortify-ish skip in ONE chunk, then is city visible same chunk?
        sid = int(settler[0])
        batch = f"""
local me = Game.GetLocalPlayer(); local p = Players[me]
local s = p:GetUnits():FindID({sid}); local w = p:GetUnits():FindID({wid})
local ok1 = UnitManager.CanStartOperation(s, UnitOperationTypes.FOUND_CITY, nil, nil)
UnitManager.RequestOperation(s, UnitOperationTypes.FOUND_CITY, {{}})
local ok2 = UnitManager.CanStartCommand(w, UnitCommandTypes.SKIP_TURN, nil) ~= nil
pcall(function() UnitManager.RequestCommand(w, UnitCommandTypes.SKIP_TURN, {{}}) end)
print("can_found=" .. tostring(ok1) .. " cities_same_chunk=" .. p:GetCities():GetCount() .. " pub=" .. ExposedMembers.AG.pub)
"""
        pub0 = await pub(t)
        t0 = time.monotonic()
        rep["batch_request"] = await t.run("InGame", batch)
        t_city, polls = None, 0
        while time.monotonic() - t0 < 5 and t_city is None:
            polls += 1
            n = int((await t.run("GameCore_Tuner", "print(Players[Game.GetLocalPlayer()]:GetCities():GetCount())"))[0])
            if n > 0:
                t_city = time.monotonic()
        ev = await t.wait_push("CityAddedToMap", 3)
        rep["batch"] = {
            "city_readable_ms": t_city and ms(t0, t_city),
            "polls": polls,
            "pub_delta": (await pub(t)) - pub0,
            "push_CityAddedToMap_ms": ev and ms(t0, ev[0]),
        }

        # Production + research + civic in ONE chunk (the usual end-turn blockers)
        setup = """
local me = Game.GetLocalPlayer(); local p = Players[me]
local c = p:GetCities():GetCapitalCity() or select(2, p:GetCities():Members()())
local prm = {}; prm[CityOperationTypes.PARAM_UNIT_TYPE] = GameInfo.Units["UNIT_SCOUT"].Hash
prm[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE
CityManager.RequestOperation(c, CityOperationTypes.BUILD, prm)
local function pick(tbl, field, can)
  for row in tbl() do if can(row) then return row end end
end
local techs = p:GetTechs(); local tr = pick(GameInfo.Technologies, "TechnologyType", function(r) return techs:CanResearch(r.Index) end)
local rp = {}; rp[PlayerOperations.PARAM_TECH_TYPE] = tr.Hash; rp[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
UI.RequestPlayerOperation(me, PlayerOperations.RESEARCH, rp)
local cul = p:GetCulture(); local cr = pick(GameInfo.Civics, "CivicType", function(r) return cul:CanProgress(r.Index) end)
local cp = {}; cp[PlayerOperations.PARAM_CIVIC_TYPE] = cr.Hash; cp[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
UI.RequestPlayerOperation(me, PlayerOperations.PROGRESS_CIVIC, cp)
print("city=" .. c:GetName() .. " tech=" .. tr.TechnologyType .. " civic=" .. cr.CivicType)
"""
        rep["setup"] = await t.run("InGame", setup)
        await asyncio.sleep(0.3)
        rep["blockers"] = await t.run("InGame", """
local me = Game.GetLocalPlayer()
print("canEnd=" .. tostring(UI.CanEndTurn()) .. " blocking=" .. tostring(NotificationManager.GetFirstEndTurnBlocking(me)))
""")

        # End turn: wait purely on pushed events (no polling)
        t.pushed.clear()
        t0 = time.monotonic()
        rep["endturn_request"] = await t.run("InGame", "UI.RequestAction(ActionTypes.ACTION_ENDTURN); print('requested turn=' .. Game.GetCurrentGameTurn())")
        ev = await t.wait_push("LocalPlayerTurnBegin", 180)
        rep["endturn"] = {
            "push_LocalPlayerTurnBegin_s": ev and round(ev[0] - t0, 2),
            "n_pushes": len(t.pushed),
            "timeline": [(round(a - t0, 2), b) for a, b in t.pushed][:40],
            "turn_now": await t.run("GameCore_Tuner", "print(Game.GetCurrentGameTurn())"),
        }

    print(json.dumps(rep, indent=1, ensure_ascii=False))
    (ROOT / "bench").mkdir(exist_ok=True)
    (ROOT / "bench" / "probe_events.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

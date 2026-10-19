"""Generic unit/city actions, done the way the game UI does them.

Listing mirrors UnitPanel.lua (GetUnitActionsTable): loop GameInfo.UnitCommands /
GameInfo.UnitOperations, ask UnitManager.CanStart* (loose test, then real test with
results), and for targeted actions (InterfaceMode set in the DB) ask
UnitManager.Get*Targets for the legal plots, as WorldInput.lua does.
Acting builds the same parameter table and calls UnitManager.Request* /
CityManager.Request*. The engine validates everything: nothing here bypasses rules.
"""

from __future__ import annotations

import json
from typing import Any

# Shared Lua: parameter encoding and result formatting. Values may be numbers or
# names: "UNIT_SCOUT" -> GameInfo.Units[...].Hash, "YIELD_GOLD" -> Yields index,
# "VALUE_APPEND" -> enum value of the same type table.
HELPERS = r"""
local me = Game.GetLocalPlayer()
local function esc(s) return (tostring(s == nil and "" or s):gsub("[|\r\n]", " ")) end
local HASHED = {"Units", "Buildings", "Districts", "Projects", "Improvements",
                "Routes", "Features", "Resources", "Beliefs", "Policies", "Governments", "GreatPersonIndividuals"}
local function encValue(enumTable, v)
  if type(v) ~= "string" then return v end
  if enumTable[v] ~= nil then return enumTable[v] end
  if v:sub(1, 6) == "YIELD_" and GameInfo.Yields[v] then return GameInfo.Yields[v].Index end
  if v:sub(1, 4) == "WMD_" and GameInfo.WMDs[v] then return GameInfo.WMDs[v].Index end
  -- UnitPanel.lua OnPromoteUnit passes the promotion Index from UnitCommandResults.PROMOTIONS
  if v:sub(1, 10) == "PROMOTION_" and GameInfo.UnitPromotions[v] then return GameInfo.UnitPromotions[v].Index end
  for _, t in ipairs(HASHED) do local r = GameInfo[t] and GameInfo[t][v]; if r then return r.Hash end end
  error("cannot encode parameter value " .. v)
end
local function encParams(enumTable, p)
  local out = {}
  for k, v in pairs(p or {}) do
    local key = enumTable[k] or enumTable["PARAM_" .. k]
    if key == nil then error("unknown parameter " .. k) end
    out[key] = encValue(enumTable, v)
  end
  return out
end
local function plotList(plots, limit)
  local s, n = {}, 0
  for _, idx in ipairs(plots or {}) do
    local pl = Map.GetPlotByIndex(idx)
    if pl then n = n + 1; if n <= limit then s[#s + 1] = pl:GetX() .. "," .. pl:GetY() end end
  end
  return table.concat(s, " "), n
end
-- Attack ops: GetOperationTargets returns every plot in range; keep only plots holding
-- something we may attack and can see (barbarians / civs at war; units or cities).
local function hostileAt(idx)
  local pl = Map.GetPlotByIndex(idx)
  if pl == nil or not PlayersVisibility[me]:IsVisible(pl:GetX(), pl:GetY()) then return false end
  local function enemy(pid)
    if pid == me or pid < 0 then return false end
    local p = Players[pid]
    if p and p:IsBarbarian() then return true end
    local ok, war = pcall(function() return Players[me]:GetDiplomacy():IsAtWarWith(pid) end)
    return ok and war
  end
  for _, u in ipairs(Units.GetUnitsInPlot(pl)) do if enemy(u:GetOwner()) then return true end end
  local d = CityManager.GetDistrictAt(pl)
  if d and enemy(d:GetOwner()) then return true end
  return false
end
local ATTACK_OPS = {UNITOPERATION_RANGE_ATTACK = true, UNITOPERATION_AIR_ATTACK = true}
local function filterHostile(plots)
  local out = {}
  for _, idx in ipairs(plots or {}) do if hostileAt(idx) then out[#out + 1] = idx end end
  return out
end
local function reasons(res)
  if res == nil then return "" end
  local r = res[UnitOperationResults.FAILURE_REASONS] or res[CityCommandResults.FAILURE_REASONS]
  if r == nil then return "" end
  local s = {}
  for _, v in ipairs(r) do s[#s + 1] = Locale.Lookup(v) end
  return esc(table.concat(s, "; "))
end
"""

UNIT_LIST = HELPERS + r"""
local u = Players[me]:GetUnits():FindID(%(uid)d)
if u == nil then print("ERR|no such unit of yours: %(uid)d") return end
local hasMoves = u:GetMovesRemaining() > 0
print("UNIT|" .. GameInfo.Units[u:GetType()].UnitType .. "|" .. u:GetX() .. "," .. u:GetY() .. "|moves=" .. u:GetMovesRemaining())
for row in GameInfo.UnitCommands() do
  if row.VisibleInUI and UnitManager.CanStartCommand(u, row.Hash, true) then
    local now, res = UnitManager.CanStartCommand(u, row.Hash, false, true)
    local extra = ""
    if row.InterfaceMode then
      local t = UnitManager.GetCommandTargets(u, row.Hash)
      if t then
        local ps, n = plotList(t[UnitCommandResults.PLOTS], %(limit)d)
        extra = "targets(" .. n .. ")=" .. ps
        local us = t[UnitCommandResults.UNITS]
        if us then for _, x in ipairs(us) do extra = extra .. " unit=" .. x.player .. ":" .. x.id end end
      end
    end
    if row.CommandType == "UNITCOMMAND_PROMOTE" then
      local ok2, r2 = UnitManager.CanStartCommand(u, row.Hash, true, true)
      local ps = {}
      for _, p in ipairs((r2 and r2[UnitCommandResults.PROMOTIONS]) or {}) do ps[#ps + 1] = GameInfo.UnitPromotions[p].UnitPromotionType end
      extra = "promotions=" .. table.concat(ps, ",")
    elseif row.CommandType == "UNITCOMMAND_UPGRADE" and res and res[UnitCommandResults.UNIT_TYPE] then
      extra = "to=" .. GameInfo.Units[res[UnitCommandResults.UNIT_TYPE]].UnitType .. " cost=" .. tostring(u:GetUpgradeCost())
    elseif row.CommandType == "UNITCOMMAND_ENTER_FORMATION" then
      local ok2, r2 = UnitManager.CanStartCommand(u, row.Hash, nil, true)
      for _, x in ipairs((r2 and r2[UnitCommandResults.UNITS]) or {}) do extra = extra .. " unit=" .. x.player .. ":" .. x.id end
    end
    print("A|command|" .. row.CommandType .. "|" .. (now and 1 or 0) .. "|" .. tostring(row.InterfaceMode ~= nil) .. "|" .. extra .. "|" .. reasons(res))
  end
end
for row in GameInfo.UnitOperations() do
  local h = row.Hash
  if row.OperationType == "UNITOPERATION_EXECUTE_SCRIPT" or row.OperationType == "UNITOPERATION_WAIT_FOR" then
    -- engine-internal, never offered by the UI
  elseif h == UnitOperationTypes.BUILD_IMPROVEMENT then
    local p = {}; p[UnitOperationTypes.PARAM_X] = u:GetX(); p[UnitOperationTypes.PARAM_Y] = u:GetY()
    local ok, res = UnitManager.CanStartOperation(u, h, nil, p, true)
    if ok and res and res[UnitOperationResults.IMPROVEMENTS] then
      local best = res[UnitOperationResults.BEST_IMPROVEMENT]
      for _, imp in ipairs(res[UnitOperationResults.IMPROVEMENTS]) do
        p[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp
        local now, r2 = UnitManager.CanStartOperation(u, h, nil, p, true)
        print("A|operation|" .. row.OperationType .. "|" .. (now and 1 or 0) .. "|false|improvement=" .. GameInfo.Improvements[imp].ImprovementType .. (imp == best and " best" or "") .. "|" .. reasons(r2))
      end
    end
  elseif h == UnitOperationTypes.MOVE_TO then
    if UnitManager.CanStartOperation(u, h, nil, false, false) then print("A|operation|UNITOPERATION_MOVE_TO|" .. (hasMoves and 1 or 0) .. "|true|x,y = any reachable plot (see get_pathing_estimate)|") end
  elseif h == UnitOperationTypes.WMD_STRIKE then
    if UnitManager.CanStartOperation(u, h, nil, true) then
      for w in GameInfo.WMDs() do
        local p = {}; p[UnitOperationTypes.PARAM_WMD_TYPE] = w.Index
        local now, res = UnitManager.CanStartOperation(u, h, nil, p, true)
        local t = UnitManager.GetOperationTargets(u, h, p)
        local ps, n = plotList(t and t[UnitOperationResults.PLOTS], %(limit)d)
        print("A|operation|UNITOPERATION_WMD_STRIKE|" .. (now and 1 or 0) .. "|true|wmd=" .. w.WeaponType .. " targets(" .. n .. ")=" .. ps .. "|" .. reasons(res))
      end
    end
  elseif UnitManager.CanStartOperation(u, h, nil, true) then
    local now, res = UnitManager.CanStartOperation(u, h, nil, false, true)
    local extra = ""
    if row.InterfaceMode then
      local t = UnitManager.GetOperationTargets(u, h)
      local plots = t and t[UnitOperationResults.PLOTS]
      if ATTACK_OPS[row.OperationType] then plots = filterHostile(plots) end
      local ps, n = plotList(plots, %(limit)d)
      extra = "targets(" .. n .. ")=" .. ps
      now = hasMoves and n > 0
    end
    print("A|operation|" .. row.OperationType .. "|" .. (now and 1 or 0) .. "|" .. tostring(row.InterfaceMode ~= nil) .. "|" .. extra .. "|" .. reasons(res))
  end
end
"""

UNIT_ACT = HELPERS + r"""
local u = Players[me]:GetUnits():FindID(%(uid)d)
if u == nil then print("ERR|no such unit of yours: %(uid)d") return end
local name = "%(action)s"
local isCmd = name:sub(1, 12) == "UNITCOMMAND_"
local row = isCmd and GameInfo.UnitCommands[name] or GameInfo.UnitOperations[name]
if row == nil then print("ERR|unknown action " .. name) return end
local enum = isCmd and UnitCommandTypes or UnitOperationTypes
local p = encParams(enum, %(params)s)
if %(x)s ~= nil then p[enum.PARAM_X] = %(x)s; p[enum.PARAM_Y] = %(y)s end
-- Moving onto an enemy unit or city is a melee attack only with the ATTACK modifier
-- (Civ6Common.lua RequestMoveOperation); without it the request is silently dropped.
-- Measured: ATTACK + MOVE_IGNORE_UNEXPLORED_DESTINATION was dropped too, ATTACK alone works.
-- Non-combat units (Great General, settler) must not carry it: their move is then dropped.
if name == "UNITOPERATION_MOVE_TO" and p[enum.PARAM_MODIFIERS] == nil and u:GetCombat() > 0 then
  p[enum.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
end
local ok, res
if isCmd then ok, res = UnitManager.CanStartCommand(u, row.Hash, p, true)
else ok, res = UnitManager.CanStartOperation(u, row.Hash, nil, p, true) end
if not ok then print("REFUSED|" .. reasons(res)) return end
if isCmd then UnitManager.RequestCommand(u, row.Hash, p) else UnitManager.RequestOperation(u, row.Hash, p) end
print("REQUESTED|" .. name)
"""

UNIT_STATE = r"""
local u = Players[Game.GetLocalPlayer()]:GetUnits():FindID(%(uid)d)
if u == nil then print("GONE") else
  local d = ""
  pcall(function() local p = UnitManager.GetQueuedDestination(u); if p then local pl = Map.GetPlotByIndex(p) or p; if pl.GetX then d = "|dest=" .. pl:GetX() .. "," .. pl:GetY() end end end)
  print(u:GetX() .. "," .. u:GetY() .. "|hp=" .. (u:GetMaxDamage() - u:GetDamage()) .. "|moves=" .. u:GetMovesRemaining() .. d)
end
"""

# What an order aimed at (x, y) can change there: visible foreign units' HP and a city/district's
# HP. Read before and after to tell an attack from a plain move and to report its outcome.
TARGET_STATE = r"""
local me = Game.GetLocalPlayer(); local x, y = %(x)d, %(y)d
if not PlayersVisibility[me]:IsVisible(x, y) then print("fog") return end
local out = {}
for _, u in ipairs(Units.GetUnitsInPlot(Map.GetPlot(x, y))) do
  if u:GetOwner() ~= me then out[#out+1] = GameInfo.Units[u:GetType()].UnitType:gsub("UNIT_", "") .. ":" .. (u:GetMaxDamage() - u:GetDamage()) end
end
local d = CityManager.GetDistrictAt(Map.GetPlot(x, y))
if d and d:GetOwner() ~= me then
  out[#out+1] = "district:" .. (d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) - d:GetDamage(DefenseTypes.DISTRICT_GARRISON)) .. "/walls:" .. (d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) - d:GetDamage(DefenseTypes.DISTRICT_OUTER))
elseif d then out[#out+1] = "mine" end
print(#out > 0 and table.concat(out, " ") or "empty")
"""

CITY_COMMANDS = ["RANGE_ATTACK", "PURCHASE", "MANAGE", "SWAP_TILE_OWNER", "SET_FOCUS", "DESTROY", "NAME_CITY", "WMD_STRIKE"]

CITY_LIST = HELPERS + r"""
local c = CityManager.GetCity(me, %(cid)d)
if c == nil then print("ERR|no such city of yours: %(cid)d") return end
print("CITY|" .. Locale.Lookup(c:GetName()) .. "|" .. c:GetX() .. "," .. c:GetY())
for _, name in ipairs(%(commands)s) do
  local h = CityCommandTypes[name]
  if h ~= nil then
    local ok = CityManager.CanStartCommand(c, h)
    local extra = ""
    local t = CityManager.GetCommandTargets(c, h, {})
    if t then
      local ps, n = plotList(t[CityCommandResults.PLOTS], %(limit)d)
      if n > 0 then extra = "targets(" .. n .. ")=" .. ps end
    end
    print("A|command|" .. name .. "|" .. (ok and 1 or 0) .. "|" .. tostring(extra ~= "") .. "|" .. extra .. "|")
  end
end
local q = c:GetBuildQueue()
local items = {}
pcall(function()
  for i = 0, q:GetSize() - 1 do
    local e = q:GetAt(i)
    local r = e and (GameInfo.Units[e.UnitType] or GameInfo.Buildings[e.BuildingType] or GameInfo.Districts[e.DistrictType] or GameInfo.Projects[e.ProjectType])
    items[#items + 1] = r and (r.UnitType or r.BuildingType or r.DistrictType or r.ProjectType) or "?"
  end
end)
print("QUEUE|" .. table.concat(items, ","))
"""

CITY_ACT = HELPERS + r"""
local c = CityManager.GetCity(me, %(cid)d)
if c == nil then print("ERR|no such city of yours: %(cid)d") return end
local name = "%(action)s"
local isOp = CityOperationTypes[name] ~= nil and CityCommandTypes[name] == nil
local enum = isOp and CityOperationTypes or CityCommandTypes
local h = enum[name]
if h == nil then print("ERR|unknown city action " .. name) return end
local p = encParams(enum, %(params)s)
local ok, res
if isOp then ok, res = CityManager.CanStartOperation(c, h, p, true) else ok, res = CityManager.CanStartCommand(c, h, p, true) end
if not ok then print("REFUSED|" .. reasons(res)) return end
if isOp then CityManager.RequestOperation(c, h, p) else CityManager.RequestCommand(c, h, p) end
print("REQUESTED|" .. name)
"""


def lua_table(d: dict | None) -> str:
    """Python dict -> Lua table constructor (strings/numbers/bools only)."""
    items = []
    for k, v in (d or {}).items():
        if isinstance(v, bool):
            val = "true" if v else "false"
        elif isinstance(v, (int, float)):
            val = repr(v)
        else:
            val = json.dumps(str(v))
        items.append(f"[{json.dumps(str(k))}]={val}")
    return "{" + ",".join(items) + "}"


def lua_list(xs: list[str]) -> str:
    return "{" + ",".join(json.dumps(x) for x in xs) + "}"


def _ident(s: str) -> str:
    if not s.replace("_", "").isalnum():
        raise ValueError(f"bad action name {s!r}")
    return s.upper()


def parse_listing(lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"actions": []}
    for l in lines:
        parts = l.split("|")
        if parts[0] == "ERR":
            raise ValueError(parts[1])
        if parts[0] in ("UNIT", "CITY"):
            out["subject"] = parts[1]
            out["at"] = parts[2]
            if len(parts) > 3:
                out["state"] = parts[3]
        elif parts[0] == "QUEUE":
            out["queue"] = [x for x in parts[1].split(",") if x]
        elif parts[0] == "A":
            a = {"kind": parts[1], "action": parts[2], "can_now": parts[3] == "1",
                 "needs_target": parts[4] == "true"}
            if parts[5]:
                a["info"] = parts[5]
            if len(parts) > 6 and parts[6]:
                a["blocked_because"] = parts[6]
            out["actions"].append(a)
    return out


def unit_list_lua(uid: int, limit: int = 30) -> str:
    return UNIT_LIST % {"uid": int(uid), "limit": int(limit)}


def unit_act_lua(uid: int, action: str, x: int | None, y: int | None, params: dict | None) -> str:
    return UNIT_ACT % {"uid": int(uid), "action": _ident(action), "params": lua_table(params),
                       "x": "nil" if x is None else int(x), "y": "nil" if y is None else int(y)}


def unit_state_lua(uid: int) -> str:
    return UNIT_STATE % {"uid": int(uid)}


def target_state_lua(x: int, y: int) -> str:
    return TARGET_STATE % {"x": int(x), "y": int(y)}


def city_list_lua(cid: int, limit: int = 30) -> str:
    return CITY_LIST % {"cid": int(cid), "limit": int(limit), "commands": lua_list(CITY_COMMANDS)}


def city_act_lua(cid: int, action: str, params: dict | None) -> str:
    return CITY_ACT % {"cid": int(cid), "action": _ident(action), "params": lua_table(params)}


# One call per turn instead of a dozen lookups: my cities, my units that can still act, visible
# enemies near them, and a combat preview of every attack each unit could make this turn.
# Previews use SimulateAttackVersus from the units' current tiles (aura as it is now); a target
# inside a city or district is simulated against the district, which is what a shot really hits.
SCAN = r"""
local me = Game.GetLocalPlayer(); local P = Players[me]; local vis = PlayersVisibility[me]
local R = %(radius)d  -- "dN" in previews = distance now; a ranged unit beyond its range must move first
local function tname(u) return GameInfo.Units[u:GetType()].UnitType:gsub("UNIT_", "") end
local function dhp(d, t) return d:GetMaxDamage(t) - d:GetDamage(t) end
print("turn " .. Game.GetCurrentGameTurn() .. " gold " .. math.floor(P:GetTreasury():GetGoldBalance()) .. " (+" .. math.floor(P:GetTreasury():GetGoldYield() - P:GetTreasury():GetTotalMaintenance()) .. ")")
for _, c in P:GetCities():Members() do
  local d = c:GetDistricts():GetDistrict(GameInfo.Districts["DISTRICT_CITY_CENTER"].Index)
  local q = c:GetBuildQueue(); local h = q:GetCurrentProductionTypeHash()
  local r = h ~= 0 and (GameInfo.Units[h] or GameInfo.Buildings[h] or GameInfo.Districts[h] or GameInfo.Projects[h])
  local g = 0; for _, u in ipairs(Units.GetUnitsInPlot(Map.GetPlot(c:GetX(), c:GetY()))) do if u:GetCombat() > 0 or u:GetRangedCombat() > 0 then g = g + 1 end end
  print("CITY " .. c:GetID() .. " " .. Locale.Lookup(c:GetName()) .. " " .. c:GetX() .. "," .. c:GetY() .. " hp " .. dhp(d, DefenseTypes.DISTRICT_GARRISON) .. "/" .. d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) .. " garrison " .. g .. " build " .. (r and (r.UnitType or r.BuildingType or r.DistrictType or r.ProjectType) or "none") .. "(" .. q:GetTurnsLeft() .. ")")
end
-- enemies: visible foreign units and cities near any of my units
local mine = {}; for _, u in P:GetUnits():Members() do mine[#mine+1] = u end
local function near(x, y, r) for _, u in ipairs(mine) do if Map.GetPlotDistance(x, y, u:GetX(), u:GetY()) <= r then return true end end return false end
local foes, fcities = {}, {}
for pid = 0, 62 do local p = Players[pid]
  if pid ~= me and p and p:IsAlive() and (p:GetDiplomacy():IsAtWarWith(me) or p:IsBarbarian()) then
    for _, u in p:GetUnits():Members() do local x, y = u:GetX(), u:GetY()
      if vis:IsVisible(x, y) and near(x, y, R) then foes[#foes+1] = u
        print("FOE " .. pid .. " " .. u:GetID() .. " " .. tname(u) .. " " .. x .. "," .. y .. " hp " .. (u:GetMaxDamage() - u:GetDamage()) .. " cs " .. u:GetCombat() .. "/" .. u:GetRangedCombat()) end
    end
    for _, c in p:GetCities():Members() do local x, y = c:GetX(), c:GetY()
      if vis:IsVisible(x, y) and near(x, y, R + 2) then
        local d = c:GetDistricts():GetDistrict(GameInfo.Districts["DISTRICT_CITY_CENTER"].Index); fcities[#fcities+1] = d
        print("FOECITY " .. pid .. " " .. Locale.Lookup(c:GetName()) .. " " .. x .. "," .. y .. " hp " .. dhp(d, DefenseTypes.DISTRICT_GARRISON) .. " walls " .. dhp(d, DefenseTypes.DISTRICT_OUTER) .. " def " .. d:GetDefenseStrength() .. " units " .. Map.GetPlot(x, y):GetUnitCount())
      end
    end
  end
end
-- my units that can still act, with previews of every reachable attack
for _, u in ipairs(mine) do
  local mv = u:GetMovesRemaining()
  if mv > 0 then
    local xp = u:GetExperience(); local promo = xp:GetExperiencePoints() >= xp:GetExperienceForNextLevel() and " PROMO" or ""
    local cs, rs = u:GetCombat(), u:GetRangedCombat()
    local line = "UNIT " .. u:GetID() .. " " .. tname(u) .. " " .. u:GetX() .. "," .. u:GetY() .. " hp " .. (u:GetMaxDamage() - u:GetDamage()) .. " mv " .. mv .. promo
    local sims = {}
    if (cs > 0 or rs > 0) and u:GetAttacksRemaining() > 0 then
      local ranged = rs > 0; local ct = ranged and CombatTypes.RANGED or CombatTypes.MELEE
      local reach = ranged and (u:GetRange() + math.ceil(mv)) or math.ceil(mv)
      local function sim(compID, x, y, label, walls)
        if Map.GetPlotDistance(u:GetX(), u:GetY(), x, y) > reach then return end
        local ok, r = pcall(CombatManager.SimulateAttackVersus, u:GetComponentID(), compID, ct)
        if ok and r then local A, D = r[CombatResultParameters.ATTACKER], r[CombatResultParameters.DEFENDER]
          local w = walls and (D[CombatResultParameters.DEFENSE_DAMAGE_TO] or 0) or 0
          sims[#sims+1] = label .. "@" .. x .. "," .. y .. " dmg " .. D[CombatResultParameters.DAMAGE_TO] .. (w > 0 and ("+walls" .. w) or "") .. " me -" .. A[CombatResultParameters.DAMAGE_TO] .. " d" .. Map.GetPlotDistance(u:GetX(), u:GetY(), x, y) end
      end
      for _, e in ipairs(foes) do
        local d = CityManager.GetDistrictAt(Map.GetPlot(e:GetX(), e:GetY()))
        if d == nil or d:GetOwner() == me then sim(e:GetComponentID(), e:GetX(), e:GetY(), tname(e)) end
      end
      for _, d in ipairs(fcities) do sim(d:GetComponentID(), d:GetX(), d:GetY(), "CITY", dhp(d, DefenseTypes.DISTRICT_OUTER) > 0) end
    end
    print(line .. (#sims > 0 and ("  ATK " .. table.concat(sims, "; ")) or ""))
  end
end
"""


def scan_lua(radius: int = 4) -> str:
    return SCAN % {"radius": int(radius)}

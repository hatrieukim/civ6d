"""Lua runtime injected into the InGame state: event handlers that print
"@@EV|seq|turn|kind|k=v|..." lines, which the tuner pushes to us unasked.

Fog of war: engine events fire for every player, including units and cities we
cannot see (measured). Everything about other players is filtered through
PlayersVisibility[me] before it is emitted.
"""

VERSION = "7"

RUNTIME_LUA = r"""
ExposedMembers.AG = ExposedMembers.AG or {}
local AG = ExposedMembers.AG
if AG.handlers then for n, f in pairs(AG.handlers) do pcall(function() Events[n].Remove(f) end) end end
AG.handlers = {}
AG.ver = "%VERSION%"
AG.seq = AG.seq or 0
AG.q = AG.q or {}
local me = Game.GetLocalPlayer()

local function vis(x, y)
  local v = PlayersVisibility[me]
  return v ~= nil and x ~= nil and x >= 0 and v:IsVisible(x, y)
end
local function revealed(x, y)
  local v = PlayersVisibility[me]
  return v ~= nil and x ~= nil and x >= 0 and v:IsRevealed(x, y)
end
local function esc(s) return (tostring(s == nil and "" or s):gsub("[|\r\n]", " ")) end
local function emit(kind, fields)
  AG.seq = AG.seq + 1
  local s = "@@EV|" .. AG.seq .. "|" .. Game.GetCurrentGameTurn() .. "|" .. kind
  for _, kv in ipairs(fields or {}) do s = s .. "|" .. kv[1] .. "=" .. esc(kv[2]) end
  AG.q[AG.seq % 300] = s
  print(s)
end
local function unitOf(pid, uid)
  local p = Players[pid]; if p == nil then return nil end
  return p:GetUnits():FindID(uid)
end
local function unitType(u)
  if u == nil then return "?" end
  local r = GameInfo.Units[u:GetType()]; return r and r.UnitType or "?"
end
local function ownerName(pid)
  local p = Players[pid]; if p == nil then return tostring(pid) end
  if p:IsBarbarian() then return "Barbarian" end
  local cfg = PlayerConfigurations[pid]
  local hasMet = pid == me or (Players[me]:GetDiplomacy():HasMet(pid))
  if not hasMet then return "Unmet" end
  return cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or tostring(pid)
end
local function nearestMyCity(x, y)
  local best, name = 999, ""
  for _, c in Players[me]:GetCities():Members() do
    local d = Map.GetPlotDistance(x, y, c:GetX(), c:GetY())
    if d < best then best, name = d, Locale.Lookup(c:GetName()) end
  end
  return best, name
end
local function on(n, f)
  if Events[n] then
    local g = function(...) local ok, err = pcall(f, ...); if not ok then emit("handler_error", {{"event", n}, {"err", err}}) end end
    Events[n].Add(g); AG.handlers[n] = g
  end
end

on("LocalPlayerTurnBegin", function() emit("my_turn") end)
on("LocalPlayerTurnEnd", function() emit("turn_end") end)

on("NotificationAdded", function(pid, nid)
  if pid ~= me then return end
  local n = NotificationManager.Find(pid, nid); if n == nil then return end
  local f = {{"type", n:GetTypeName()}, {"msg", Locale.Lookup(n:GetMessage() or "")}}
  local ok, sum = pcall(function() return Locale.Lookup(n:GetSummary() or "") end)
  if ok and sum ~= "" then f[#f+1] = {"summary", sum} end
  if n:IsLocationValid() then local x, y = n:GetLocation(); f[#f+1] = {"x", x}; f[#f+1] = {"y", y} end
  emit("notification", f)
end)

AG.seen = {}
on("UnitVisibilityChanged", function(pid, uid, state)
  if pid == me or state ~= RevealedState.VISIBLE then return end
  local u = unitOf(pid, uid); if u == nil or not vis(u:GetX(), u:GetY()) then return end
  local key = pid .. ":" .. uid .. "@" .. u:GetX() .. "," .. u:GetY()   -- fires several times per sighting
  if AG.seen[key] == Game.GetCurrentGameTurn() then return end
  AG.seen[key] = Game.GetCurrentGameTurn()
  local d, cname = nearestMyCity(u:GetX(), u:GetY())
  emit("unit_seen", {{"owner", ownerName(pid)}, {"unit", unitType(u)}, {"id", uid}, {"x", u:GetX()}, {"y", u:GetY()}, {"near_city", cname}, {"dist", d}})
end)

local function hostile(pid)
  local p = Players[pid]; if p == nil then return false end
  if p:IsBarbarian() then return true end
  local ok, war = pcall(function() return Players[me]:GetDiplomacy():IsAtWarWith(pid) end)
  return ok and war
end
on("UnitMoved", function(pid, uid, x, y)
  if pid == me or not vis(x, y) or not hostile(pid) then return end  -- passers-by at peace are not alarms
  local d, cname = nearestMyCity(x, y)
  if d <= 3 then
    emit("unit_near_city", {{"owner", ownerName(pid)}, {"unit", unitType(unitOf(pid, uid))}, {"id", uid}, {"x", x}, {"y", y}, {"city", cname}, {"dist", d}})
  end
end)

on("UnitDamageChanged", function(pid, uid, newDamage, oldDamage)
  if pid ~= me or newDamage <= oldDamage then return end
  local u = unitOf(pid, uid)
  emit("my_unit_damaged", {{"unit", unitType(u)}, {"id", uid}, {"hp", 100 - newDamage}, {"x", u and u:GetX()}, {"y", u and u:GetY()}})
end)

on("UnitKilledInCombat", function(killedPid, killedUid, killerPid, killerUid)
  if killedPid == me then emit("my_unit_killed", {{"id", killedUid}, {"by", ownerName(killerPid)}})
  elseif killerPid == me then emit("enemy_unit_killed", {{"owner", ownerName(killedPid)}, {"by_id", killerUid}}) end
end)

on("CityAddedToMap", function(pid, cid, x, y)
  if pid == me then
    local c = CityManager.GetCity(pid, cid)
    emit("city_added", {{"city", c and Locale.Lookup(c:GetName())}, {"id", cid}, {"x", x}, {"y", y}})
  elseif revealed(x, y) then
    emit("city_seen", {{"owner", ownerName(pid)}, {"x", x}, {"y", y}})
  end
end)

-- Our cities and encampments being hit (a galley raiding a city produced no event before).
on("DistrictDamageChanged", function(pid, did, dtype, newDamage, oldDamage)
  if pid ~= me or newDamage <= oldDamage then return end
  local d = Players[me]:GetDistricts():FindID(did); if d == nil then return end
  local c = d:GetCity(); local dt = GameInfo.Districts[d:GetType()]
  local maxhp = d:GetMaxDamage(dtype)
  emit("my_city_damaged", {{"city", c and Locale.Lookup(c:GetName())}, {"district", dt and dt.DistrictType},
    {"defense", dtype == DefenseTypes.DISTRICT_OUTER and "walls" or "garrison"},
    {"hp", maxhp - newDamage}, {"max", maxhp}, {"x", d:GetX()}, {"y", d:GetY()}})
end)

on("CityRemovedFromMap", function(pid, cid)
  if pid == me then emit("my_city_removed", {{"id", cid}}) end
end)

on("CityProductionCompleted", function(pid, cid, orderType, objType, canceled)
  if pid ~= me or canceled then return end
  local c = CityManager.GetCity(pid, cid)
  -- objType indexes the table named by orderType (TutorialUIRoot.lua: 0 train, 1 construct, 2 zone)
  local tbl = ({[0] = {GameInfo.Units, "UnitType"}, [1] = {GameInfo.Buildings, "BuildingType"},
                [2] = {GameInfo.Districts, "DistrictType"}, [3] = {GameInfo.Projects, "ProjectType"}})[orderType]
  local r = tbl and tbl[1][objType]
  local name = r and r[tbl[2]] or ("order" .. tostring(orderType) .. ":" .. tostring(objType))
  emit("production_done", {{"city", c and Locale.Lookup(c:GetName())}, {"id", cid}, {"item", name}})
end)

on("ResearchCompleted", function(pid, tech)
  if pid == me then local r = GameInfo.Technologies[tech]; emit("research_done", {{"tech", r and r.TechnologyType}}) end
end)
on("CivicCompleted", function(pid, civic)
  if pid == me then local r = GameInfo.Civics[civic]; emit("civic_done", {{"civic", r and r.CivicType}}) end
end)

on("DiplomacyDeclareWar", function(a, b)
  if a == me or b == me then emit("war_declared", {{"by", ownerName(a)}, {"on", ownerName(b)}}) end
end)
on("DiplomacyMakePeace", function(a, b)
  if a == me or b == me then emit("peace", {{"a", ownerName(a)}, {"b", ownerName(b)}}) end
end)
on("CityOccupationChanged", function(pid, cid)
  if pid == me then emit("my_city_occupation_changed", {{"id", cid}}) end
end)

-- Completion signals for our own orders (civ6d waits on these instead of sleeping):
-- the combat animation ends with CombatVisEnd, a move with UnitMoveComplete.
local function member(v, i, alt)
  local m = v and (v[i] or v[alt]); if type(m) ~= "table" then return nil end
  return m
end
local function combat(kind)
  return function(v)
    local a = member(v, CombatVisType.ATTACKER, 1); local d = member(v, CombatVisType.DEFENDER, 2)
    local ap, ai = a and a.playerID, a and a.componentID
    local dp, di = d and d.playerID, d and d.componentID
    if ap ~= me and dp ~= me then return end
    emit(kind, {{"att_pid", ap}, {"att_id", ai}, {"def_pid", dp}, {"def_id", di}})
  end
end
on("CombatVisBegin", combat("combat_begin"))
on("CombatVisEnd", combat("combat_end"))
on("UnitMoveComplete", function(pid, uid, x, y)
  if pid == me then emit("move_done", {{"id", uid}, {"x", x}, {"y", y}}) end
end)
on("UnitMovementPointsChanged", function(pid, uid)
  if pid == me then emit("my_moves_changed", {{"id", uid}}) end
end)

-- Tech/civic boost & completed popups hold the UI (seen: BoostUnlockedPopup open while
-- ENDTURN blockers lingered). The game's own tutorial switch turns them off.
pcall(function() LuaEvents.TutorialUIRoot_DisableTechAndCivicPopups() end)

function AG.since(n)
  for i = n + 1, AG.seq do local s = AG.q[i % 300]; if s then print(s) end end
end
print("runtime " .. AG.ver .. " installed, seq=" .. AG.seq)
""".replace("%VERSION%", VERSION)

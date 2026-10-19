"""Dump the Lua API the running game exposes (globals, enums, object methods)
into docs/03-api-map.md and docs/api-map.json. Needs civ6d running and a game loaded.

Run: uv run --project civ6-mcp python tools/dump_api.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from civ6d.paths import ASSETS  # noqa: E402

URL = "http://127.0.0.1:7790/v1/lua"

# The game's Lua states are sandboxed (no _G, getfenv or debug), so globals cannot be
# enumerated. Instead every capitalised identifier used as `Name.` / `Name:` / `Name[`
# in the shipped UI scripts is probed by name.


def candidates() -> list[str]:
    import re
    names: set[str] = set()
    pat = re.compile(r"(?<![.:\w])([A-Z][A-Za-z0-9_]*)\s*[.:\[(]")
    for f in ASSETS.rglob("*.lua"):
        names.update(pat.findall(f.read_text(errors="ignore")))
    return sorted(names)


GLOBALS = r"""
local C = {}
%s
local names = {}
for k in pairs(C) do names[#names + 1] = k end
table.sort(names)
for _, k in ipairs(names) do
  local v = C[k]; local t = type(v)
  if t == "table" then
    local fns, vals = {}, {}
    for kk, vv in pairs(v) do
      if type(kk) == "string" then
        if type(vv) == "function" then fns[#fns + 1] = kk else vals[#vals + 1] = kk end
      end
    end
    table.sort(fns); table.sort(vals)
    if #vals > 120 then vals = {"(" .. #vals .. " members)"} end
    print("G|" .. k .. "|table|" .. table.concat(fns, ",") .. "|" .. table.concat(vals, ","))
  else
    print("G|" .. k .. "|" .. t .. "||")
  end
end
"""

OBJECTS = r"""
local me = Game.GetLocalPlayer(); local P = Players[me]
local c, u
for _, x in P:GetCities():Members() do c = c or x end
for _, x in P:GetUnits():Members() do u = u or x end
local getters = {
  {"Player", function() return P end},
  {"PlayerCities", function() return P:GetCities() end},
  {"PlayerUnits", function() return P:GetUnits() end},
  {"PlayerTechs", function() return P:GetTechs() end},
  {"PlayerCulture", function() return P:GetCulture() end},
  {"PlayerTreasury", function() return P:GetTreasury() end},
  {"PlayerDiplomacy", function() return P:GetDiplomacy() end},
  {"PlayerReligion", function() return P:GetReligion() end},
  {"PlayerResources", function() return P:GetResources() end},
  {"PlayerStats", function() return P:GetStats() end},
  {"PlayerInfluence", function() return P:GetInfluence() end},
  {"PlayerGovernors", function() return P:GetGovernors() end},
  {"PlayerEras", function() return P:GetEras() end},
  {"PlayerTrade", function() return P:GetTrade() end},
  {"PlayerAi", function() return P:GetAi_Military() end},
  {"PlayerConfiguration", function() return PlayerConfigurations[me] end},
  {"PlayerVisibility", function() return PlayersVisibility[me] end},
  {"City", function() return c end},
  {"CityBuildQueue", function() return c:GetBuildQueue() end},
  {"CityBuildings", function() return c:GetBuildings() end},
  {"CityDistricts", function() return c:GetDistricts() end},
  {"CityGrowth", function() return c:GetGrowth() end},
  {"CityCitizens", function() return c:GetCitizens() end},
  {"CityCulture", function() return c:GetCulture() end},
  {"CityReligion", function() return c:GetReligion() end},
  {"CityTrade", function() return c:GetTrade() end},
  {"CityGold", function() return c:GetGold() end},
  {"Unit", function() return u end},
  {"UnitExperience", function() return u:GetExperience() end},
  {"UnitAbility", function() return u:GetAbility() end},
  {"Plot", function() return Map.GetPlot(c and c:GetX() or u:GetX(), c and c:GetY() or u:GetY()) end},
  {"District", function() for _, d in c:GetDistricts():Members() do return d end end},
}
for _, g in ipairs(getters) do
  local ok, o = pcall(g[2])
  if ok and o ~= nil then
    local fns = {}
    local function collect(t) if type(t) == "table" then for k, v in pairs(t) do if type(v) == "function" and type(k) == "string" then fns[k] = true end end end end
    collect(o)
    local mt = getmetatable(o)
    if mt then collect(mt); collect(mt.__index) end
    local list = {}
    for k in pairs(fns) do if k:sub(1, 2) ~= "__" then list[#list + 1] = k end end
    table.sort(list)
    print("O|" .. g[1] .. "|" .. table.concat(list, ","))
  else
    print("O|" .. g[1] .. "|(unavailable)")
  end
end
"""


def lua(state: str, code: str) -> list[str]:
    req = urllib.request.Request(URL, data=json.dumps({"state": state, "code": code, "timeout": 30}).encode(),
                                 headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    if not r.get("ok"):
        raise RuntimeError(r)
    return r["result"]


def parse(lines: list[str]) -> dict:
    out = {"globals": {}, "objects": {}}
    for l in lines:
        p = l.split("|")
        if p[0] == "G":
            out["globals"][p[1]] = {"type": p[2], "functions": [x for x in p[3].split(",") if x],
                                    "members": [x for x in p[4].split(",") if x]}
        elif p[0] == "O":
            out["objects"][p[1]] = [x for x in p[2].split(",") if x]
    return out


def md(api: dict) -> str:
    L = ["# Civ VI Lua API map (runtime)", "",
         f"Dumped by `tools/dump_api.py` on {time.strftime('%Y-%m-%d %H:%M')} from a running game "
         "(Civ VI v1.4.6 Aspyr, Gathering Storm). Raw data: `docs/api-map.json`.", "",
         "- **InGame** (UI state): reads + legal orders, as the player gives them (`UnitManager.Request*`, "
         "`CityManager.Request*`, `UI.RequestPlayerOperation`...). civ6d gives its orders here.",
         "- **GameCore_Tuner** (core state): fast reads; also has functions that *write state directly* (spawn units, "
         "transfer cities...). **Never use those — it is cheating** (CLAUDE.md).", "",
         "To learn how a function is used: `grep -rn 'FunctionName' <Civ6.app>/Contents/Assets --include='*.lua'` — "
         "the game's own UI is the reference example.", ""]
    for state, data in api.items():
        L += [f"## {state}", "", "### Objects (methods)", ""]
        for name, fns in data["objects"].items():
            L.append(f"- **{name}** ({len(fns)}): {', '.join(fns) if fns else '—'}")
        libs = {k: v for k, v in data["globals"].items() if v["type"] == "table" and v["functions"]}
        enums = {k: v for k, v in data["globals"].items() if v["type"] == "table" and not v["functions"] and v["members"]}
        others = {k: v["type"] for k, v in data["globals"].items() if v["type"] != "table"}
        L += ["", f"### Global libraries with functions ({len(libs)})", ""]
        for k, v in libs.items():
            L.append(f"- **{k}** ({len(v['functions'])}): {', '.join(v['functions'])}")
        L += ["", f"### Enum / data tables ({len(enums)})", ""]
        for k, v in enums.items():
            L.append(f"- **{k}**: {', '.join(v['members'])}")
        L += ["", f"### Other values ({len(others)})", "", ", ".join(f"{k} ({t})" for k, t in others.items()), ""]
    return "\n".join(L)


def main() -> None:
    api = {}
    names = candidates()
    print(len(names), "candidate global names from UI scripts")
    for state in ("InGame", "GameCore_Tuner"):
        lines = []
        for i in range(0, len(names), 400):
            chunk = "\n".join(f'C["{n}"] = {n}' for n in names[i:i + 400])
            lines += lua(state, GLOBALS % chunk)
        api[state] = parse(lines + lua(state, OBJECTS))
        g = api[state]["globals"]
        print(state, len(g), "globals,", sum(len(v["functions"]) for v in g.values()), "library functions,",
              sum(len(v) for v in api[state]["objects"].values()), "object methods")
    (ROOT / "docs" / "api-map.json").write_text(json.dumps(api, indent=1, ensure_ascii=False))
    (ROOT / "docs" / "03-api-map.md").write_text(md(api))


if __name__ == "__main__":
    main()

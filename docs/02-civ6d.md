# civ6d — the Civ VI control daemon (v0.2.1, 2026-10-18)

## 1. What it is
A background Python process (`python -m civ6d`). It holds **one** long-lived FireTuner connection to the game and serves HTTP on `127.0.0.1:7790`. Claude calls it through `tools/civ6`.

```
Claude ── tools/civ6 ── HTTP 127.0.0.1:7790 ── civ6d ── TCP 127.0.0.1:4318 (FireTuner) ── Civ VI
                                                  ▲ game events pushed back (@@EV|...)
```

- **Reads and orders:** reuses all of civ6-mcp's `GameState` (102 methods, already tested). The connection layer is replaced by `civ6d/conn.py`: no fixed waits, about 18 ms per command instead of 320 ms.
- **Events:** `civ6d/runtime.py` attaches handlers to `Events.*` in the `InGame` state. Each event is `print`ed as `@@EV|seq|turn|kind|k=v...` and the tuner pushes it to us. News about other civs is **filtered by visibility** (`PlayersVisibility[me]`), so nothing leaks through the fog of war.
- **Single connection:** while civ6d is connected, port 4318 no longer shows as LISTEN (only 4319). That is normal: the tuner takes one client at a time. To run your own script, `tools/civ6 stop` first.

## 2. Starting the game and loading (no clicks)
```bash
tools/civ6-launch                         # run Civ6_Exe_Child directly: skips the Aspyr launcher, waits for the tuner (~6 s)
tools/civ6 start                          # start the daemon (connects to the tuner at the menu)
tools/civ6 post load name=0A_GROUND_CONTROL   # load from the menu: gets past the Continue screen by itself (~30 s)
```
Measured: game closed → turn 1 ready in **40 s**.

- **Loading from inside a game:** set `Automation.SetAutoStartEnabled(true)` before loading and the Continue screen presses itself (`LoadScreen.lua:OnLoadGameViewStateDone`). About 20 s.
- **Loading from the menu:** once the `InGame` state appears, enable autostart. If the screen is waiting (`InputContext.Ready`), fire `Events.LoadGameViewStateDone()` again.
- **Important:** while the game is loading, **do not hold the socket or send `LSQ:`**. The game rebuilds the tuner on every load ("Reloading Tuner states"). civ6d disconnects, waits 8 s, then reconnects. The old way (keep the socket, keep listing states) jammed the tuner, leaving only an empty 4319 until the game was restarted.
- Options: `EnableTuner 1` is required (`tools/setup` sets it); `PlayIntroVideo 0` makes launching faster. With `QuickCombat` / `QuickMovement` at 1, AI turns are faster.

## 3. Turn loop
```bash
tools/civ6 brief            # 1 call (~0.25 s): turn, economy, end-turn blocker, cities, units (<READY = needs orders),
                            # visible threats, notifications, events since the last brief
tools/civ6 brief 1          # fuller version
tools/civ6 post batch 'ops=[{"op":"found_city","unit_id":65536},{"op":"fortify_unit","unit_id":131073}]'
tools/civ6 post advance turns=10        # end turns back to back, stop on a blocker or when something happens
tools/civ6 ops              # op list + signatures (unit_id is converted to unit_index for you)
tools/civ6 post call op=get_city_production city_id=65536
```

### advance stops when
- **There is an end-turn blocker:** `ENDTURN_BLOCKING_PRODUCTION`, `RESEARCH`, `CIVIC`, `UNITS` (units with moves left and no orders), `GOVERNOR`, `PROMOTION`…
- **An event in `DEFAULT_STOP` arrives:** `war_declared`, `peace`, `my_unit_damaged`, `my_unit_killed`, `unit_near_city` (hostile unit within 3 tiles of a city), `city_seen`, `my_city_removed`, `my_city_damaged`, `my_city_occupation_changed`, `handler_error`.
- **A notification arrives** whose type contains one of: DIPLOMACY, DECLARE_WAR, BARBARIAN, CITY_RANGE_ATTACK, CAPTURED, LOST, SPY, EMERGENCY, PLAYER_MET, WORLD_CONGRESS, DISASTER, LOYALTY, REBELLION.
- **The game hands the turn back with a diplomacy session waiting.** Seen when meeting a new civ for the first time: the game returns the turn a few times. civ6d resends end-turn up to 4 times if there is no session.

Change the stop conditions: `stop=war_declared,my_unit_damaged` (replaces the default list).

Measured: each early-game turn takes 0.3–2 s. 8 turns in 9.5 s (scout on auto-explore), stopping exactly when a city needed production.

## 4. v0.2 — every legal action, game rules, API map

### Generic unit actions (same as the game's unit panel, `UnitPanel.lua`)
```bash
tools/civ6 get unit/actions unit_id=131073
# -> [{action: UNITOPERATION_FORTIFY, can_now: true}, {action: UNITOPERATION_RANGE_ATTACK, needs_target: true,
#      info: "targets(2)=45,20 46,19"}, {action: UNITCOMMAND_PROMOTE, info: "promotions=..."}, ...,
#     blocked_because: the reason the game gives when it cannot be done yet]
tools/civ6 post unit/act unit_id=131073 action=UNITOPERATION_PILLAGE
tools/civ6 post unit/act unit_id=131073 action=RANGE_ATTACK x=45 y=20          # no prefix -> UNITOPERATION_
tools/civ6 post unit/act unit_id=65537 action=UNITOPERATION_BUILD_IMPROVEMENT 'params={"IMPROVEMENT_TYPE":"IMPROVEMENT_FARM"}'
tools/civ6 post unit/act unit_id=131073 action=UNITCOMMAND_PROMOTE 'params={"PROMOTION_TYPE":"PROMOTION_BATTLECRY"}'
```
- The list walks all of `GameInfo.UnitCommands` (28) and `GameInfo.UnitOperations` (63), minus `EXECUTE_SCRIPT`/`WAIT_FOR` (engine internals). An action with an `InterfaceMode` in the DB needs a target; legal plots come from `UnitManager.Get*Targets`, as in `WorldInput.lua`.
- `act` asks `CanStart*` with the exact parameters first; if not allowed it returns `REFUSED|reason` and sends nothing. Otherwise it calls `Request*`, then reads back position/HP/moves/destination after ~60 ms.
- `params`: keys are `PARAM_*` names (with or without the prefix), values are numbers or names: `UNIT_*`/`BUILDING_*`/`DISTRICT_*`/`PROJECT_*`/`IMPROVEMENT_*`/`PROMOTION_*`… → Hash; `YIELD_*`, `WMD_*` → Index; `VALUE_APPEND`… → enum.
- Measured: listing ~13 ms, an order ~20–100 ms, a refusal ~27 ms.

### Cities
```bash
tools/civ6 get city/actions city_id=65536      # available commands + target plots + build queue
tools/civ6 post city/act city_id=65536 action=BUILD 'params={"UNIT_TYPE":"UNIT_SLINGER","INSERT_MODE":"VALUE_APPEND"}'
tools/civ6 post city/act city_id=65536 action=PURCHASE 'params={"UNIT_TYPE":"UNIT_WARRIOR","YIELD_TYPE":"YIELD_GOLD"}'
tools/civ6 post city/act city_id=65536 action=SET_FOCUS 'params={"YIELD_TYPE":"YIELD_PRODUCTION"}'
tools/civ6 post city/act city_id=65536 action=RANGE_ATTACK 'params={"X":45,"Y":20}'
```
Tested: BUILD (EXCLUSIVE then APPEND → 2-item queue), PURCHASE without enough gold → REFUSED, SET_FOCUS. Not tested: MANAGE (lock a citizen on a tile, needs `PARAM_MANAGE_CITIZEN` as in `PlotInfo.lua`), SWAP_TILE_OWNER, RANGE_ATTACK, WMD_STRIKE.

All 4 ops also work inside `batch`: `{"op":"unit_act",...}`, `unit_actions`, `city_act`, `city_actions`.

### Game rules without game calls
```bash
tools/civ6 rules "SELECT UnitType, Cost, Combat, PrereqTech FROM Units WHERE Combat > 0 ORDER BY Cost"
tools/civ6 get rules/tables like=District          # 428 tables
tools/civ6 get rules/describe table=Buildings
tools/civ6 get rules/text tag=LOC_UNIT_SCOUT_NAME  # -> "Scout"
```
Source: `Cache/DebugGameplay.sqlite`, which the game rewrites every time it sets up a game (exactly the ruleset of the current game: base + enabled DLC). Opened read-only, one SELECT/WITH/PRAGMA statement, at most 200 rows by default.

### API map
`docs/03-api-map.md` (+ `docs/api-map.json`), dumped with `uv run --project civ6-mcp python tools/dump_api.py`. The game's Lua states are sandboxed (no `_G`/`getfenv`/`debug`), so the tool tries each of the 6,700 capitalised names the UI Lua uses. Result: InGame 237 globals, 757 library functions, 707 object methods; GameCore_Tuner 142 / 550 / 510. GameCore has functions that write state directly (`InitUnit`, `TransferCity`, `ChangeDamage`…): **forbidden**.

### Fixed in v0.2
- A Lua chunk that `return`ed early lost the end marker → every refused order waited 10 s. Code is now wrapped in `(function() ... end)()`.

### v0.2.1 (after playtest 1, details in `docs/04-playtest-1.md`)
- `advance`: closes popups that hold the turn (wonders, disasters...) by itself; AI talking mid-turn → stops with `diplomacy_sessions`, except greetings/compliments that need no decision (`auto_ack_diplomacy=true` by default, logged in `diplomacy_acknowledged`); calling it again while the AI is still playing just keeps waiting.
- Safe `set_civic`/`set_research`: if the target is out of reach, picks a prerequisite and reports `PREREQ_FIRST`.
- `call op=combat_estimate unit_id= target_x= target_y=` — the game's own simulation, all modifiers included.
- Event filtering: `unit_near_city` only for hostile units; `unit_seen` without repeats; meeting a city-state does not stop.
- CLI: `tools/civ6 adv N` prints advance results compactly.
- Every API call is logged to `logs/requests.jsonl`.

## 5. Saves and safety
- `tools/civ6 post save name=X`: never overwrites an existing file unless `overwrite=true`.
- civ6d does **not** autosave `0_MCP_*`, does **not** quit or restart the game by itself, does **not** open any port to the LAN.
- `POST /v1/lua` is an escape hatch (arbitrary Lua). Never use it to cheat or see through the fog; see CLAUDE.md.

## 6. Known gaps / not right yet
- Peace deals that cede cities / great works (DiplomacyDealView screen) have no generic command yet; locking citizens on tiles is untested.
- civ6-mcp's attack functions still `sleep` 0.3–0.5 s.
- `UnitVisibilityChanged` may report the same unit several times as it moves in and out of sight.
- No standing orders of our own yet. For now we rely on the game's: fortify/sentry/sleep, multi-turn moves, auto-explore, research queue.
- If the game connection drops, civ6d reconnects on the next call. After a load, the Lua runtime is reinstalled.
- Scenarios (e.g. Conquests of Alexander, `RULESET_SCENARIO_ALEXANDER`) turn some systems off: no `Player:GetFavor`, no `Game.GetEras()` methods, no `City:GetCulturalIdentity`. Guarded in civ6-mcp's `overview.py` and `cities.py` (`patches/civ6-mcp-scenario-rulesets.patch`) so `brief` works. Tech/civic/government/diplomacy/era ops (`tech.py`, `governance.py`, `diplomacy.py`) are not guarded, since that scenario has none of those systems.
- `unit_act` MOVE_TO now adds `UnitOperationMoveModifiers.ATTACK` (as `Civ6Common.lua` does), so moving into a tile with an enemy unit/city is a melee attack. Measured: without the flag the order is silently ignored; `ATTACK + MOVE_IGNORE_UNEXPLORED_DESTINATION` is ignored too.
- Shooting a unit inside a district (encampment, city center) hits the district; the unit inside takes no damage. `combat_estimate` still simulates it as shooting the unit, so its numbers are wrong in that case.
- `resolve_city_capture` guards `GetNextRebelledCity` (missing in scenarios).
- The ATTACK flag is only added for units with combat strength (`u:GetCombat() > 0`); on a Great General the move is ignored. `unit_act` still reports `REQUESTED` when the destination is unreachable (e.g. a tile already holding a civilian of the same class): check `get_pathing_estimate` (turns=-1) first.
- With normal animations, combat results show up after ~5 s; reading earlier shows old numbers.
- `unit_act` with MOVE_TO / RANGE_ATTACK / AIR_ATTACK / PILLAGE… waits for a completion signal instead of a fixed sleep. Runtime v6 emits `combat_begin`/`combat_end` (`CombatVisBegin/End`), `move_done` (`UnitMoveComplete`), `my_moves_changed`. The result has a `waited` field: `combat_done`, `move_done`, `done` or `nothing_happened` (6 s without a signal — animations queue up so an order may start 3 s late; if the unit's state did not change, `status=IGNORED`, ok=false). Measured: one ranged shot with normal animations takes ~5 s (4.1 s animation + 0.8 s settle). These signals are hidden from brief/advance/events, except `GET /v1/events?signals=true`.
- **civ6-mcp's `promote_unit` is blocked (cheat):** it writes the promotion with `SetPromotion` in GameCore without raising the level (so the game allows promoting again), then `SetDamage(0)` heals fully. Real rule: a promotion heals 50 HP (`EXPERIENCE_PROMOTE_HEALED`). The right path is `UnitManager.RequestCommand(u, UnitCommandTypes.PROMOTE, {PARAM_PROMOTION_TYPE = index})` (UnitPanel.lua), now available through `unit_act` (encParams turns `PROMOTION_*` into an Index). `BLOCKED_OPS` in `core.py` blocks the op and hides it from `/v1/ops`.
- Runtime v7: `my_city_damaged` event (`Events.DistrictDamageChanged`, our city/encampment losing HP: city, district, hp/max, walls or garrison), and `advance` stops on it by default. Before that, an enemy ship hit Athens 3 turns in a row (200 → 92) without any event. `brief` now prints `hp…/…` per city, with `!` when a city is damaged.

# Playtest 1 — 0A_GROUND_CONTROL, turns 1 → 33 (2026-10-18)

Babylon (Hammurabi), Pangaea, Prince, Quick speed. Interim strategy: science the Babylon way (eurekas grant whole techs).
Data sources: `logs/requests.jsonl` (one line per API call), `logs/civ6d.log`.

## Game result
T33: 3 cities (Babylon pop 3, Eshnunna pop 2, Rapiqum pop 1), Campus + Library (Library granted by Babylon's ability), science 4.5, gold 147.
Met: Rome, Japan, 3 city-states. Eureka for Writing from meeting Rome (Babylon gets the whole tech). Fights with barbarians: warrior vs warrior, vs horseman; bought a slinger for 90 gold to hold the capital.

## Calls and time
| Metric | Value |
|---|---|
| Turns played | 32 (T1 → T33) |
| civ6d API calls | 146: call 38, advance 32, batch 26, brief 25, status 9, other 16 |
| Claude tool rounds (merging requests close together) | ~82, i.e. **~2.6 per turn** |
| Wall-clock time for the session | 22.3 min, of which ~20 min was Claude thinking **and fixing 14 daemon bugs along the way** |
| Non-advance calls | median **85 ms**, 38 s total for 114 calls |
| `advance` per turn (AI turns + popup/diplomacy handling) | usually 2.4–4 s; 6–12 s with popups/diplomacy |
| `advance` stuck before the fixes | 466 s (diplomacy session during AI turns), 158 s (natural wonder popup) |

Estimate without bug fixing: ~2.6 rounds × ~10–15 s thinking + ~3 s game ≈ **30–45 s per turn**. Many turns fold into one `advance` (runs until something needs attention), but early on almost every turn brings something new (a new unit, a finished civic/tech).

## Bugs found and fixed (v0.2.1)
| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `load` from inside a game fails with a Lua error | game states have no `_G` (sandbox) | read the save enums directly |
| 2 | "production_done UNIT_BUILDER" reported on founding a city | item looked up in Units regardless of `orderType` | look up by `orderType` (0 unit, 1 building, 2 district, 3 project) — it was actually the Palace |
| 3 | `advance` hangs | "Natural Wonder Discovered" popup (input `Reveal`) holds the turn | while waiting, close popups every 4 s with civ6-mcp's `dismiss_popup` |
| 4 | `set_policies` reports success but slots stay empty | JSON only has string keys, civ6-mcp wants `dict[int,str]` | convert keys to int |
| 5 | batch reports `ok` with `SILENT_FAILURE` / `Error:` on a later line | only the start of the string was checked | regex over every line + `SILENT_FAILURE` |
| 6 | `advance` waits 466 s | AI opens a diplomacy session during AI turns | stop and return `diplomacy_sessions`; calling `advance` again just keeps waiting |
| 7 | Every AI greeting/compliment stops the run | — | `auto_ack_diplomacy` (on by default): messages with no deal/choice/war are answered POSITIVE and logged in `diplomacy_acknowledged` |
| 8 | `CIVIC` blocker never clears | civ6-mcp `set_civic(EARLY_EMPIRE)` without Foreign Trade still reports PROGRESSING | `set_civic`/`set_research` check `CanProgress/CanResearch`; if not, pick the nearest prerequisite (BFS over the prereq tables in the DB) and report `PREREQ_FIRST` |
| 9 | Tech/civic boost popups | — | the runtime calls `LuaEvents.TutorialUIRoot_DisableTechAndCivicPopups()` (the game's own switch); an open popup is closed with `OnClose()` inside the popup's own Lua state |
| 10 | Wrong combat estimate (24 vs 44 actual) | civ6-mcp ignores modifiers (Discipline) | `combat_estimate` op uses `CombatManager.SimulateAttackVersus`, like the game's preview |
| 11 | `unit_seen` repeated many times | the engine fires the event several times | de-duplicate by (unit, plot, turn) |
| 12 | `unit_near_city` stops for a city-state unit passing by | hostility not checked | only barbarians / civs at war |
| 13 | Meeting a city-state stops the run | `PLAYER_MET` | ignored for city-states |
| 14 | `RANGE_ATTACK` lists 7 empty plots | `GetOperationTargets` returns every plot in range | keep only plots with a visible enemy |
| — | refused orders take 10 s | early `return` lost the end marker | wrap the chunk in a function (fixed in v0.2) |

## Still known to be off
- `production_done` fires twice for districts.
- `REFUSED` for building an improvement gives no reason (the engine returns no `FAILURE_REASONS`); you have to infer it (e.g. forest still there → chop first).
- MOVE_TO onto a city tile already holding a military unit: the unit stops midway every turn, with no explanation.
- `attack_unit` (civ6-mcp) still prints the old estimate without modifiers — check `combat_estimate` before attacking.
- civ6-mcp's settle advisor says "no valid settle locations within 5 tiles" and then suggests a tile 3 tiles away.
- Early on, each new unit / each finished tech-civic stops `advance`: correct (a decision is needed), but it is the main source of calls. A tech/civic queue and standing orders for new units would reduce it.

# CLAUDE.md — playing Civilization VI

This project lets Claude play Civilization VI (Steam/Aspyr, macOS) for the user, without the GUI, through the `civ6d` daemon that talks to the game over FireTuner. Read `docs/02-civ6d.md` before doing anything; measurements and design reasons are in `docs/01-baseline.md`. Strategy is set by the user in `strategy.md` (template: `strategy.example.md`); every decision follows that file. If it does not exist, ask the user how they want to play. Personal settings (language, journal, strategy file name) may be in `CLAUDE.local.md`.

## Starting up
1. `tools/civ6 status` — no answer: `tools/civ6 start`. `in_game=false`: the game is at the main menu.
2. Game not running: `tools/civ6-launch` (skips the Aspyr launcher), then `tools/civ6 start`.
3. At the menu: `tools/civ6 saves`, then `tools/civ6 post load name=...` with the save the user picks (it gets past the Continue screen by itself). Do not start a new game on your own.

## Turn loop
1. `tools/civ6 brief` — one call: turn, economy, end-turn blocker, cities, units (`<READY` = needs orders), visible threats, notifications, new events.
2. Handle the blocker and notifications marked `!` first.
3. Decide according to `strategy.md`. Look before acting: `get unit/actions unit_id=` (every legal action + target plots + why blocked), `get city/actions city_id=`, `post call op=get_city_production`, `get_settle_advisor`, `get_district_advisor`, `get_trade_options`, `get_pathing_estimate`… (`tools/civ6 ops` lists them). Look up rules (costs, strength, requirements) with `tools/civ6 rules "SELECT ..."` — costs no game call.
4. Give the whole turn's orders in **one** `post batch ops=[...]` (`unit_act`/`city_act` can do every action the UI can). Check each op's `ok`; `REFUSED` = the game said no, read `detail`.
5. `tools/civ6 adv N` (or `post advance turns=N`) — N = 1–3 at war, 5–20 at peace. It stops by itself on blockers or events; read `stopped_because`. On `diplomacy session waiting`, read `diplomacy_sessions`, answer with `post call op=diplomacy_respond other_player_id= response=POSITIVE|NEGATIVE`, then `adv` again. Before attacking: `post call op=combat_estimate`.
6. Report briefly to the user: what happened, what you did, key numbers. Stop and ask before big decisions: declaring war, signing peace, changing government/religion, forming alliances, using nuclear weapons.

## Hard rules
- No cheating: never use `post lua` to change game state (add gold, units, reveal the map…), never use the console/cheats. `post lua` is only for reading or for doing exactly what the UI can do.
- civ6-mcp ops may write game state directly. `promote_unit` is blocked: it calls `SetPromotion` + `SetDamage(0)`, so units fully heal and can be promoted forever. Promote with `unit_act action=UNITCOMMAND_PROMOTE params={"PARAM_PROMOTION_TYPE":"PROMOTION_X"}`. Read the Lua of any new civ6-mcp op before using it; if it calls `Set*`/`Change*`/`Init*`/`Kill` in GameCore, add it to `BLOCKED_OPS` (`civ6d/core.py`).
- Do not see through fog of war: only use information the player can see. civ6d events are already filtered; your own Lua must filter by `PlayersVisibility[me]` too.
- Do not overwrite the user's existing saves without consent (`post save` refuses without `overwrite=true`); use a new name.
- Irreversible actions need the user's OK first: `UNITCOMMAND_DELETE`, city `DESTROY`, razing cities, `WMD_STRIKE`, declaring war.
- Do not hold a second tuner connection while civ6d runs (the tuner accepts one client); for your own scripts run `tools/civ6 stop` first.
- Do not quit the game while the user is mid-game without asking.
- Changing the daemon: code is in `civ6d/`, apply with `tools/civ6 stop && tools/civ6 start`. API lookup: `docs/03-api-map.md`. Game mechanics: the original Lua in `<Civ6.app>/Contents/Assets/` (see `civ6d/paths.py`), read/command library in `civ6-mcp/src/civ_mcp/`.

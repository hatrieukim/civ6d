# civ6-mcp baseline and a faster channel to the game (2026-10-18)

Machine: Mac arm64, Civ VI Steam/Aspyr v1.4.6 (buildid 15296837), with Gathering Storm. Test save: `0A_GROUND_CONTROL` (Babylon, Pangaea, Prince, Quick speed, 330 turns), turns 1–5. civ6-mcp commit `dd20190` (v1.1.11).
Raw numbers: `bench/baseline.json`, `bench/probe_events.json`. Scripts: `tools/`.

## 1. Setup
- `AppOptions.txt`: `EnableTuner 1`, `PlayIntroVideo 0` (`tools/setup` enables the tuner and keeps a backup).
- The tuner only listens on `127.0.0.1:4318` (and `:4319`); it is not exposed to the LAN.
- Copied civ6-mcp's 3 sample saves into `~/Library/Application Support/Sid Meier's Civilization VI/Sid Meier's Civilization VI/Saves/Single/`. civ6-mcp created an extra `0_MCP_0004.Civ6Save` while measuring end_turn.
- Starting the game from Steam goes through the Aspyr launcher (PLAY button) and then the "Continue Game" screen after a load. Both needed a manual click at this point (solved later, see `02-civ6d.md`).

## 2. Measurements
| Task | Time |
|---|---|
| One raw Lua call (send + wait for sentinel), GameCore or InGame | **18 ms** (median, max 27) |
| Same call through civ6-mcp `GameConnection.execute_read` | **320 ms** — fixed 0.1 s wait before + 0.2 s after every command |
| civ6-mcp read tools (turn 1): overview / units / map r4 | 1.27 s / 0.96 s / 0.96 s |
| civ6-mcp read tools: cities, tech, diplomacy, strategic map, victory, notifications | ~0.32 s each |
| Output size on turn 1: tech_civics / victory / map r4 | ~1260 / ~440 / ~260 tokens (grows a lot later) |
| Loading a save with Lua from the menu (`Network.LoadGame`) until the InGame state exists | ~114 s |
| Ending a turn with civ6-mcp `execute_end_turn` (turn 3→4) | **9.87 s** (snapshot, turn polling, diff, `0_MCP_*` autosave) |
| Ending a turn with `UI.RequestAction(ACTION_ENDTURN)` + waiting for a pushed event (turn 4→5) | **1.29 s** (turn 1→2: 1.47 s) |
| Ordering every idle unit in one Lua chunk | 16 ms |

The model's thinking time between calls is not measured here. It is still the largest part (estimated 5–15 s per call).

## 3. Results of the 4 probes
1. **Event handlers injected over the tuner: WORK.** `Events.X.Add(f)` sent into the `InGame` state keeps firing across turns. Handlers are kept in `ExposedMembers.AG` so they can be removed and re-added without duplicates. Not tested at the time: whether they survive loading another save (expected not; re-install after a load).
2. **print() is pushed unasked: WORKS.** `print()` inside a handler reaches the socket without a request. UnitMoved arrives after 42 ms. Ending a turn pushes 169–183 events, the last being `LocalPlayerTurnBegin`. No turn polling needed.
3. **Batching orders: WORKS.** One Lua chunk founded a city and skipped a warrior's turn. Another set production + research + civic, after which `canEnd=true`.
4. **How long to wait:** inside the same chunk the result is not visible yet (old position, 0 cities). The next read (~34 ms, 1–7 `GameCoreEventPublishComplete`) sees it. Dependent orders just need two chunks, not the 0.3–0.5 s sleeps civ6-mcp uses.

## 4. Side findings
- **Fog of war:** `UnitMoved`, `CityAddedToMap`, `PlayerTurnActivated`… fire for every player, including AI units and cities we cannot see. Handlers must filter with `PlayersVisibility[me]:IsVisible(x, y)` before pushing, or they see through the fog.
- **Single connection:** after a disconnect the tuner takes a few seconds to accept a new connection (an immediate reconnect is refused). The daemon has to hold one long-lived connection.
- Every UI screen is its own Lua state reachable over the tuner (123 states in game: `InGame`, `GameCore_Tuner`, `DiplomacyActionView`, `LoadGameMenu`…).
- The menu state (`Main State`) has `io`, `os`, `Network.LoadGame`. Enums live in `AppUIEnumsLibrary` / `LuaNetworkEnumsLibrary`. Loading a save from the menu needs no OCR clicking.
- "Continue Game" screen: the button calls `Events.LoadScreenClose()` (`Base/Assets/UI/FrontEnd/LoadScreen.lua:47`).
- The Advisor popup (new players) makes the end-turn button show "PLEASE WAIT" but does not block `RequestAction`.

## 5. Design conclusions
- Drop the fixed waits: each command goes from 320 ms to 18 ms.
- End turns on a pushed event: from ~10 s to ~1.3 s (early game, AI has little to do).
- A daemon holds the connection, installs the (fog-filtered) handlers after every load, reads the event stream continuously, and serves `brief` / `batch` / `advance` over localhost HTTP. It reuses civ6-mcp's state-reading Lua and rewrites the connection layer and end_turn.

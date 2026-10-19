# civ6d — let Claude play Civilization VI

`civ6d` is a small local daemon that lets [Claude Code](https://claude.com/claude-code) play a real game of Civilization VI for you: no screenshots, no mouse clicks, no cheats. It talks to the running game over FireTuner (the game's built-in debug channel). Every order goes through the same engine checks the game UI uses, so Claude plays by the rules and only sees what you could see.

```
Claude Code ── tools/civ6 ── HTTP 127.0.0.1:7790 ── civ6d ── TCP 127.0.0.1:4318 (FireTuner) ── Civ VI
                                                       ▲ game events pushed back, filtered by fog of war
```

It is built on [civ6-mcp](https://github.com/lmwilki/civ6-mcp) by Liam Wilkinson, which provides the Lua that reads game state and gives orders. civ6d replaces its connection layer and adds what a long game needs:

- **Fast.** One persistent connection instead of fixed waits. A command takes ~18 ms instead of ~320 ms, and ending a turn takes ~1.3 s instead of ~10 s ([measurements](docs/01-baseline.md)).
- **Pushed events.** Lua handlers injected into the game report combat, sightings, finished production, war declarations and so on as they happen. News about other players is filtered by your visibility, so nothing leaks through the fog of war.
- **Few round trips.** `brief` sums up the whole turn in one call, `batch` gives every order of a turn in one call, and `advance N` ends turns until something needs a decision (a blocker, an attack, a diplomacy request…).
- **Every legal action.** `unit/actions` and `city/actions` list exactly what the game's unit and city panels offer, with valid targets and the reason an action is blocked.
- **Rules lookup at no cost.** Read-only SQL over the ruleset of the loaded game (units, buildings, techs, modifiers…).
- **No cheating by design.** civ6-mcp ops that write game state directly are blocked, raw Lua is documented as read-or-UI-only, saves are never overwritten silently, and irreversible actions need your OK ([CLAUDE.md](CLAUDE.md)).

## Requirements

- macOS with Civilization VI from Steam (Aspyr port, tested on v1.4.6 with Gathering Storm). Other platforms may work with `CIV6_APP` / `CIV6_USER_DIR` set (see [`civ6d/paths.py`](civ6d/paths.py)), but are untested.
- [Claude Code](https://claude.com/claude-code)
- [uv](https://docs.astral.sh/uv/), git, Python 3.12+ (uv can install it)

## Install

```bash
git clone --recurse-submodules https://github.com/hatrieukim/civ6d.git
cd civ6d
tools/setup
```

`tools/setup` fetches civ6-mcp (pinned to a tested commit), applies a small [patch](patches/civ6-mcp-scenario-rulesets.patch), installs the Python dependencies, and sets `EnableTuner 1` in the game's `AppOptions.txt`. It backs that file up to `AppOptions.txt.before-civ6d` first. The game must be closed while it runs, and it must have been started at least once before.

Optional: civ6-mcp ships three test saves. To try them, copy them into your save folder:

```bash
cp civ6-mcp/evals/saves/*.Civ6Save "$HOME/Library/Application Support/Sid Meier's Civilization VI/Sid Meier's Civilization VI/Saves/Single/"
```

## Play

1. Optional: write your strategy. Copy [`strategy.example.md`](strategy.example.md) to `strategy.md` and edit it. Claude follows it for every decision.
2. Open Claude Code in this folder and say something like *"load 0A_GROUND_CONTROL and play"*. [CLAUDE.md](CLAUDE.md) tells Claude how to start the game (`tools/civ6-launch`, which skips the Aspyr launcher) and the daemon, and how to play a turn.
3. Claude reports after each stretch of turns and stops to ask before big decisions: war, peace, alliances, government or religion changes, nuclear weapons, deleting units or razing cities.

You can also drive the daemon yourself:

```bash
tools/civ6 start                     # start the daemon
tools/civ6 brief                     # what is happening this turn
tools/civ6 get unit/actions unit_id=131073
tools/civ6 post batch 'ops=[{"op":"unit_act","unit_id":131073,"action":"UNITOPERATION_FORTIFY"}]'
tools/civ6 adv 5                     # end up to 5 turns, stop when something needs attention
tools/civ6 rules "SELECT UnitType, Cost, Combat FROM Units WHERE Combat > 0 ORDER BY Cost LIMIT 10"
tools/civ6 help                      # every endpoint
```

Personal settings that should not be committed (the language Claude answers in, a differently named strategy file, journals) go in `CLAUDE.local.md`, which git ignores.

## Layout

| Path | What |
|---|---|
| `civ6d/` | the daemon: connection (`conn.py`), injected event runtime (`runtime.py`), turn logic (`core.py`), generic unit/city actions (`actions.py`), rules DB (`rules.py`), HTTP API (`server.py`) |
| `tools/civ6` | CLI for the daemon |
| `tools/civ6-launch`, `tools/setup` | start the game without the Aspyr launcher; one-time setup |
| `tools/*.py` | probes and benchmarks used to design civ6d |
| `docs/` | [design and measurements](docs/01-baseline.md), [daemon reference](docs/02-civ6d.md), [Lua API map](docs/03-api-map.md), [first playtest](docs/04-playtest-1.md) |
| `civ6-mcp/` | git submodule ([upstream](https://github.com/lmwilki/civ6-mcp)) |
| `patches/` | changes applied on top of civ6-mcp |
| `tests/` | offline tests (`uv run --project civ6-mcp pytest tests`) |

## Known limits

See [docs/02-civ6d.md §6](docs/02-civ6d.md#6-known-gaps--not-right-yet). In short: macOS only so far, some deal screens (ceding cities, great works) have no generic command yet, and civ6-mcp's own estimates ignore some modifiers (use `combat_estimate`).

## Credits and license

- [civ6-mcp](https://github.com/lmwilki/civ6-mcp) © Liam Wilkinson, MIT license.
- civ6d is released under the [MIT license](LICENSE).

This is an unofficial fan project. It is not affiliated with or endorsed by Firaxis Games, 2K, Take-Two Interactive or Aspyr. Sid Meier's Civilization is a trademark of Take-Two Interactive Software. You need your own copy of the game; no game files are included.

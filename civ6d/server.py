"""HTTP API (127.0.0.1 only). Every response: {"ok": true, "result": ...} or
{"ok": false, "error": "...", "detail": ...}."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import rules
from .paths import SAVE_DIR
from .core import DEFAULT_STOP, Civ6D, jsonable

log = logging.getLogger("civ6d.http")
D = Civ6D()

HELP = {
    "GET /v1/status": "connection, turn, end-turn blocker, popup, input context",
    "POST /v1/reconnect": "reconnect tuner and reinstall the event runtime",
    "GET /v1/brief?full=0|1": "compact turn summary + events since the last brief (text)",
    "GET /v1/events?since=N&signals=false": "pushed events after id N (fog-filtered); signals=true adds order-completion signals",
    "GET /v1/ops": "batch/call ops = civ6-mcp GameState methods with signatures",
    "POST /v1/call {op, ...args}": "one op; unit_id is accepted where unit_index is expected",
    "POST /v1/batch {ops:[{op,...}], stop_on_error=true}": "several ops in order, one HTTP call",
    "POST /v1/end_turn {timeout=600}": "end turn, wait for pushed my_turn, return events",
    "POST /v1/advance {turns, stop=[kinds]}": f"end up to N turns; stops on blockers or events (default {DEFAULT_STOP})",
    "GET /v1/unit/actions?unit_id=": "every legal action of one unit now (as the unit panel shows), targets, why blocked",
    "POST /v1/unit/act {unit_id, action, x?, y?, params?}": "do UNITOPERATION_*/UNITCOMMAND_* (engine-checked first)",
    "GET /v1/city/actions?city_id=": "city commands with target plots + build queue",
    "POST /v1/city/act {city_id, action, params}": "BUILD/PURCHASE/RANGE_ATTACK/SET_FOCUS/MANAGE/... with PARAM_* values",
    "GET /v1/rules?sql=SELECT...&limit=200": "read-only query of the loaded ruleset (DebugGameplay.sqlite), no game call",
    "GET /v1/rules/tables?like=": "rule tables; /v1/rules/describe?table=Units for columns; /v1/rules/text?tag=LOC_...",
    "GET /v1/saves": "single-player saves on disk",
    "POST /v1/load {name}": "load a save (from inside a game: auto-continues the load screen)",
    "POST /v1/save {name, overwrite=false}": "save; refuses to overwrite an existing file unless overwrite=true",
    "POST /v1/lua {state=InGame|GameCore_Tuner|..., code}": "raw Lua escape hatch (no cheats: see CLAUDE.md)",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await D.conn.connect()
        if D.conn.ingame_index is not None:
            await D.ensure()
    except Exception as e:  # noqa: BLE001
        log.warning("startup connect failed (game not running?): %s", e)
    yield
    await D.conn.disconnect()


app = FastAPI(lifespan=lifespan)
REQ_LOG = Path(__file__).resolve().parent.parent / "logs" / "requests.jsonl"


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One line per API call: time, path, ms, game turn — for per-turn cost reports."""
    t0 = time.perf_counter()
    response = await call_next(request)
    try:
        import json as _json
        turn = D.events[-1]["turn"] if D.events else None
        REQ_LOG.parent.mkdir(exist_ok=True)
        with REQ_LOG.open("a") as f:
            f.write(_json.dumps({"t": round(time.time(), 3), "path": request.url.path, "status": response.status_code,
                                 "ms": round((time.perf_counter() - t0) * 1000), "turn": turn}) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return response


def ok(result: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "result": jsonable(result)})


@app.exception_handler(Exception)
async def on_error(_req: Request, e: Exception):
    log.exception("request failed")
    return JSONResponse({"ok": False, "error": type(e).__name__, "message": str(e)[:2000]}, status_code=500)


async def body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = {}
    return {**dict(req.query_params), **(b or {})}


@app.get("/v1/help")
async def help_():
    return ok(HELP)


@app.get("/v1/status")
async def status():
    return ok(await D.status())


@app.post("/v1/reconnect")
async def reconnect():
    return ok(await D.reconnect())


@app.get("/v1/brief")
async def brief(full: int = 0):
    return ok(await D.brief(bool(full)))


@app.get("/v1/events")
async def events(since: int = 0, signals: bool = False):
    return ok(D.events_since(since, signals))


@app.get("/v1/ops")
async def ops():
    return ok(D.ops())


@app.post("/v1/call")
async def call(req: Request):
    b = await body(req)
    op = b.pop("op")
    return ok(await D.call(op, b))


@app.post("/v1/batch")
async def batch(req: Request):
    b = await body(req)
    stop = str(b.get("stop_on_error", True)).lower() not in ("false", "0")
    return ok(await D.batch(b["ops"], stop))


@app.post("/v1/end_turn")
async def end_turn(req: Request):
    b = await body(req)
    return ok(await D.end_turn(float(b.get("timeout", 600))))


@app.post("/v1/advance")
async def advance(req: Request):
    b = await body(req)
    stop = b.get("stop")
    if isinstance(stop, str):
        stop = [s for s in stop.split(",") if s]
    ack = str(b.get("auto_ack_diplomacy", True)).lower() not in ("false", "0")
    return ok(await D.advance(int(b.get("turns", 1)), stop, float(b.get("timeout", 600)), ack))


@app.post("/v1/lua")
async def lua(req: Request):
    b = await body(req)
    return ok(await D.conn.run(b.get("state", "InGame"), b["code"], float(b.get("timeout", 10))))


@app.get("/v1/unit/actions")
async def unit_actions(unit_id: int, limit: int = 30):
    return ok(await D.unit_actions(unit_id, limit))


@app.post("/v1/unit/act")
async def unit_act(req: Request):
    b = await body(req)
    return ok(await D.unit_act(int(b["unit_id"]), b["action"], b.get("x"), b.get("y"), b.get("params")))


@app.get("/v1/city/actions")
async def city_actions(city_id: int, limit: int = 30):
    return ok(await D.city_actions(city_id, limit))


@app.post("/v1/city/act")
async def city_act(req: Request):
    b = await body(req)
    return ok(await D.city_act(int(b["city_id"]), b["action"], b.get("params")))


@app.get("/v1/rules")
async def rules_query(sql: str, limit: int = 200):
    return ok(rules.query(sql, limit))


@app.get("/v1/rules/tables")
async def rules_tables(like: str = ""):
    return ok(rules.tables(like))


@app.get("/v1/rules/describe")
async def rules_describe(table: str):
    return ok(rules.describe(table))


@app.get("/v1/rules/text")
async def rules_text(tag: str):
    return ok(rules.text(tag))


@app.get("/v1/saves")
async def saves():
    files = sorted(SAVE_DIR.glob("*.Civ6Save"), key=os.path.getmtime, reverse=True)
    return ok([{"name": f.stem, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))}
               for f in files])


QUERY_SAVES = """
-- Menu state keeps the enums in AppUIEnumsLibrary; InGame has them as globals (no _G there).
local E = AppUIEnumsLibrary or {SaveLocations = SaveLocations, SaveTypes = SaveTypes, SaveLocationOptions = SaveLocationOptions}
ExposedMembers.CD_saves = nil
local function h(fl, qid) ExposedMembers.CD_saves = fl; UI.CloseFileListQuery(qid); LuaEvents.FileListQueryResults.Remove(h) end
LuaEvents.FileListQueryResults.Add(h)
UI.QuerySaveGameList(E.SaveLocations.LOCAL_STORAGE, E.SaveTypes.SINGLE_PLAYER, E.SaveLocationOptions.NORMAL + E.SaveLocationOptions.LOAD_METADATA)
"""


PASS_LOAD_SCREEN = """
if not Automation.IsAutoStartEnabled() then Automation.SetAutoStartEnabled(true) end
if Input.GetActiveContext() == InputContext.Ready then Events.LoadGameViewStateDone() end
"""


@app.post("/v1/load")
async def load(req: Request):
    """In game: set Automation autostart (the load screen then continues by itself),
    LoadGame, drop the socket while the game rebuilds the tuner, reconnect.
    From the main menu the tuner has often not come back after the load
    (docs/01-baseline.md) — prefer loading from inside a game."""
    name = (await body(req))["name"].removesuffix(".Civ6Save")
    await D.conn.ensure_connected()
    in_game = D.conn.ingame_index is not None
    state = "InGame" if in_game else "Main State"
    if in_game:
        await D.conn.run(state, "Automation.SetAutoStartEnabled(true)")
    await D.conn.run(state, QUERY_SAVES)
    idx = None
    for _ in range(50):
        lines = await D.conn.run(state, "for i, s in ipairs(ExposedMembers.CD_saves or {}) do print(i .. '|' .. s.Name) end")
        idx = next((l.split("|")[0] for l in lines if l.split("|")[1].removesuffix(".Civ6Save") == name), None)
        if lines:
            break
        await asyncio.sleep(0.1)
    if idx is None:
        return JSONResponse({"ok": False, "error": "save_not_found", "message": name}, status_code=404)
    server_type = "ServerType" if in_game else "LuaNetworkEnumsLibrary.ServerType"
    await D.conn.run(state, f"Network.LoadGame(ExposedMembers.CD_saves[{idx}], {server_type}.SERVER_TYPE_NONE)")
    D.runtime_ok = False
    await D.conn.disconnect()
    t0 = time.monotonic()
    await asyncio.sleep(8)
    last = None
    while time.monotonic() - t0 < 240:
        try:
            await D.conn.connect(attempts=4)
            if D.conn.ingame_index is not None:
                # Loaded from the menu: autostart was not set before the load screen
                # appeared. Set it and replay the "load done" event so the screen's own
                # handler presses Continue (LoadScreen.lua OnLoadGameViewStateDone).
                await D.conn.run("InGame", PASS_LOAD_SCREEN)
                st = await D.status()
                last = st
                if st.get("input") in ("World", "Universal"):
                    await D.ensure()
                    return ok({"loaded": name, "seconds": round(time.monotonic() - t0, 1), "status": await D.status()})
            await D.conn.disconnect()
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        await asyncio.sleep(3)
    return JSONResponse({"ok": False, "error": "load_timeout", "detail": last}, status_code=504)


@app.post("/v1/save")
async def save(req: Request):
    b = await body(req)
    name = b["name"].removesuffix(".Civ6Save")
    if (SAVE_DIR / f"{name}.Civ6Save").exists() and str(b.get("overwrite", "")).lower() not in ("true", "1"):
        return JSONResponse({"ok": False, "error": "exists", "message": f"{name} exists; pass overwrite=true"},
                            status_code=409)
    from civ_mcp.game_lifecycle import save_game
    await D.ensure()
    return ok(await save_game(D.conn, name))

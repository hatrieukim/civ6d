"""Daemon state and the fast-path commands: brief, batch, end_turn, advance."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import time
from typing import Any

from civ_mcp.game_state import GameState

from . import actions as A
from .conn import FastConn
from .runtime import RUNTIME_LUA, VERSION

log = logging.getLogger("civ6d")

# Events that stop `advance` by default (kinds emitted by runtime.py).
DEFAULT_STOP = [
    "war_declared", "peace", "my_unit_damaged", "my_unit_killed", "unit_near_city",
    "my_city_removed", "my_city_damaged", "my_city_occupation_changed", "city_seen", "handler_error",
]
# Notification types that also stop `advance` (substring match on type name).
STOP_NOTIFICATIONS = [
    "DIPLOMACY", "DECLARE_WAR", "BARBARIAN", "CITY_RANGE_ATTACK", "CAPTURED", "LOST",
    "SPY", "EMERGENCY", "PLAYER_MET", "WORLD_CONGRESS", "DISASTER", "LOYALTY", "REBELLION",
]

# civ6-mcp result strings: failures start a line with Error:/ERR|/FAIL/CANNOT/INVALID,
# sometimes after a multi-line estimate (attack_unit).
import re as _re
BAD_RESULT = _re.compile(r"(?m)^\s*(error|err|fail(ed)?|cannot|invalid)\b", _re.I)

STATUS_LUA = """
local me = Game.GetLocalPlayer()
local b = NotificationManager.GetFirstEndTurnBlocking(me)
local bn = "NONE"
for k, v in pairs(EndTurnBlockingTypes) do if v == b and b ~= EndTurnBlockingTypes.NO_ENDTURN_BLOCKING then bn = k end end
local c = Input.GetActiveContext(); local cn = "?"
for k, v in pairs(InputContext) do if v == c then cn = k end end
print("turn=" .. Game.GetCurrentGameTurn() .. "|blocking=" .. bn .. "|can_end=" .. tostring(UI.CanEndTurn())
  .. "|turn_active=" .. tostring(Players[me]:IsTurnActive()) .. "|popup=" .. tostring(UI.IsAnyPopupInterfaceVisible())
  .. "|input=" .. cn .. "|runtime=" .. tostring(ExposedMembers.AG and ExposedMembers.AG.ver))
"""

READY_UNITS_LUA = """
local p = Players[Game.GetLocalPlayer()]
for _, u in p:GetUnits():Members() do
  if u:GetX() >= 0 then
    local act = "?"
    pcall(function() local a = UnitManager.GetActivityType(u); for k, v in pairs(ActivityTypes) do if v == a then act = k end end end)
    print(u:GetID() .. "|" .. act .. "|" .. tostring(u:GetMovesRemaining() > 0))
  end
end
"""


# civ6-mcp ops that change game state directly instead of doing what the UI does (cheats).
BLOCKED_OPS = {
    "promote_unit": "civ6-mcp writes the promotion in GameCore (SetPromotion, no level-up, so the unit can be "
                    "promoted again and again) and fully heals it with SetDamage(0). Use unit_act "
                    "action=UNITCOMMAND_PROMOTE params={PARAM_PROMOTION_TYPE: PROMOTION_X} (UnitPanel.lua path).",
}

# Order-completion signals from runtime.py: used by unit_act, hidden from brief/advance/events.
SIGNALS = {"combat_begin", "combat_end", "move_done", "my_moves_changed"}
# Orders that play an animation; unit_act waits for it to finish before reading the result.
WAITED_ACTIONS = {"UNITOPERATION_MOVE_TO", "UNITOPERATION_RANGE_ATTACK", "UNITOPERATION_AIR_ATTACK",
                  "UNITOPERATION_COASTAL_RAID", "UNITOPERATION_SWAP_UNITS", "UNITOPERATION_PILLAGE",
                  "UNITOPERATION_WMD_STRIKE"}


def jsonable(x: Any) -> Any:
    if dataclasses.is_dataclass(x):
        return jsonable(dataclasses.asdict(x))
    if isinstance(x, (set, frozenset)):
        return sorted(x, key=str)
    if isinstance(x, (list, tuple)):
        return [jsonable(i) for i in x]
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    return x


def parse_event(line: str) -> dict:
    # @@EV|seq|turn|kind|k=v|...
    parts = line.split("|")
    ev: dict[str, Any] = {"seq": int(parts[1]), "turn": int(parts[2]), "kind": parts[3]}
    for kv in parts[4:]:
        k, _, v = kv.partition("=")
        ev[k] = int(v) if v.lstrip("-").isdigit() else v
    return ev


class Civ6D:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._ev_signal = asyncio.Event()
        self.conn = FastConn(on_event=self._on_event)
        self.gs = GameState(self.conn)
        self.brief_mark = 0  # event seq shown by the last brief
        self._n = 0
        self._last_lseq = 0
        self.runtime_ok = False
        self.popups_dismissed: list[dict] = []
        self.diplo_acked: list[dict] = []
        self.auto_ack = True

    # -- events -------------------------------------------------------------
    def _on_event(self, line: str) -> None:
        try:
            ev = parse_event(line)
        except (IndexError, ValueError):
            log.warning("bad event line: %s", line)
            return
        if ev["seq"] <= self._last_lseq:
            return  # replay (AG.since) of something we already have
        self._last_lseq = ev["seq"]
        ev["lseq"] = ev["seq"]
        self._n += 1
        ev["seq"] = self._n  # daemon-wide id; Lua seq restarts after a save load
        ev["t"] = round(time.time(), 2)
        self.events.append(ev)
        del self.events[:-2000]
        self._ev_signal.set()

    def events_since(self, seq: int, signals: bool = False) -> list[dict]:
        """Events after `seq`; order-completion signals (SIGNALS) only when asked for."""
        return [e for e in self.events if e["seq"] > seq and (signals or e["kind"] not in SIGNALS)]

    async def wait_event(self, kinds: set[str], after_seq: int, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            for e in self.events_since(after_seq, signals=True):
                if e["kind"] in kinds:
                    return e
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._ev_signal.clear()
            try:
                await asyncio.wait_for(self._ev_signal.wait(), left)
            except asyncio.TimeoutError:
                return None

    def last_seq(self) -> int:
        return self.events[-1]["seq"] if self.events else 0

    # -- connection / runtime ----------------------------------------------
    async def ensure(self) -> None:
        """Connected, in a game, runtime installed."""
        if not self.conn.is_connected:
            # a new connection may be a restarted game whose Lua states have no runtime yet:
            # without this, events silently stopped after a game restart
            self.runtime_ok = False
        await self.conn.ensure_connected()
        if self.conn.ingame_index is None:
            await self.conn.connect()
        if self.conn.ingame_index is None:
            raise ConnectionError("no game loaded")
        if not self.runtime_ok:
            self._last_lseq = 0
            r = await self.conn.execute_write(RUNTIME_LUA)
            self.runtime_ok = any("installed" in l for l in r)
            log.info("runtime: %s", r)

    async def reconnect(self) -> dict:
        self.runtime_ok = False
        await self.conn.connect()
        if self.conn.ingame_index is not None:
            await self.ensure()
        return await self.status()

    async def status(self) -> dict:
        from . import __version__
        out: dict[str, Any] = {"version": __version__, "connected": self.conn.is_connected, "lua_states": len(self.conn.lua_states),
                               "in_game": self.conn.ingame_index is not None, "last_event_seq": self.last_seq()}
        if out["in_game"] and out["connected"]:
            line = (await self.conn.execute_write(STATUS_LUA))[0]
            out.update(dict(kv.split("=", 1) for kv in line.split("|")))
            out["runtime_ok"] = out.get("runtime") == VERSION
        return out

    # -- brief --------------------------------------------------------------
    async def brief(self, full: bool = False) -> str:
        await self.ensure()
        gs = self.gs
        ov = await gs.get_game_overview()
        st = await self.status()
        units = await gs.get_units()
        cities, _ = await gs.get_cities()
        try:
            threats = await gs.get_threat_scan()
        except Exception:  # noqa: BLE001
            threats = []
        notifs = await gs.get_notifications()
        acts = {}
        for l in await self.conn.execute_write(READY_UNITS_LUA):
            uid, act, moves = l.split("|")
            acts[int(uid)] = (act.replace("ACTIVITY_", "").lower(), moves == "true")

        L: list[str] = []
        L.append(f"T{ov.turn}{'/' + str(ov.max_turns) if ov.max_turns else ''} {ov.civ_name} ({ov.leader_name}) "
                 f"era={ov.era_name} score={ov.score} diff={ov.difficulty}")
        L.append(f"gold {ov.gold:.0f} ({ov.gold_per_turn:+.0f}) sci {ov.science_yield:.1f} cul {ov.culture_yield:.1f} "
                 f"faith {ov.faith:.0f} favor {ov.diplomatic_favor} ({ov.favor_per_turn:+d}) pop {ov.total_population} "
                 f"explored {ov.explored_land}/{ov.total_land}")
        L.append(f"research {ov.current_research} | civic {ov.current_civic}")
        L.append(f"end-turn: blocking={st.get('blocking')} can_end={st.get('can_end')} popup={st.get('popup')}")
        L.append(f"cities ({len(cities)}): id name pop food+/grow prod build(turns) def hp[/walls] loyalty")
        for c in cities:
            L.append(f"  {c.city_id} {c.name} p{c.population} {c.food_surplus:+.1f}/{c.turns_to_grow}t "
                     f"{c.production:.1f} {c.currently_building}({c.production_turns_left}) "
                     f"def{c.defense_strength} hp{c.garrison_hp}/{c.garrison_max_hp}"
                     f"{'!' if c.garrison_hp < c.garrison_max_hp else ''}"
                     f"{f' walls{c.wall_hp}/{c.wall_max_hp}' if c.wall_max_hp else ''} loy{c.loyalty:.0f}{'%+.0f' % c.loyalty_per_turn if c.loyalty_per_turn else ''}"
                     f"{' targets=' + ','.join(c.attack_targets) if c.attack_targets else ''}")
        L.append(f"units ({len(units)}): id type x,y hp moves activity")
        for u in units:
            act, has_moves = acts.get(u.unit_id, ("?", False))
            flag = " <READY" if has_moves and act in ("awake", "?") else ""
            extra = []
            if u.build_charges:
                extra.append(f"charges={u.build_charges}")
            if u.needs_promotion:
                extra.append("PROMOTE")
            if u.can_upgrade:
                extra.append(f"upg->{u.upgrade_target}({u.upgrade_cost})")
            if u.targets:
                extra.append("targets=" + ",".join(u.targets[:4]))
            L.append(f"  {u.unit_id} {u.unit_type.replace('UNIT_', '')} {u.x},{u.y} {u.health}/{u.max_health} "
                     f"{u.moves_remaining:g}/{u.max_moves:g} {act}{flag} {' '.join(extra)}".rstrip())
        if threats:
            L.append(f"visible threats ({len(threats)}): type owner x,y hp cs/rs dist")
            for t in sorted(threats, key=lambda t: t.distance)[: (50 if full else 12)]:
                L.append(f"  {t.unit_type.replace('UNIT_', '')} {t.owner_name} {t.x},{t.y} {t.hp}/{t.max_hp} "
                         f"{t.combat_strength}/{t.ranged_strength} d{t.distance}")
        if notifs:
            L.append(f"notifications ({len(notifs)}):")
            for n in notifs[: (60 if full else 15)]:
                loc = f" @{n.x},{n.y}" if n.x >= 0 else ""
                L.append(f"  {'!' if n.is_action_required else ' '} {n.type_name.replace('NOTIFICATION_', '')}: "
                         f"{n.message}{loc}{' -> ' + n.resolution_hint if n.resolution_hint else ''}")
        evs = self.events_since(self.brief_mark)
        if evs:
            counts: dict[str, int] = {}
            for e in evs:
                counts[e["kind"]] = counts.get(e["kind"], 0) + 1
            L.append("events since last brief: " + ", ".join(f"{k}x{v}" for k, v in counts.items()))
            for e in evs[-(40 if full else 12):]:
                if e["kind"] in ("my_turn", "turn_end"):
                    continue
                rest = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("seq", "turn", "kind", "t", "lseq"))
                L.append(f"  T{e['turn']} {e['kind']} {rest}")
        self.brief_mark = self.last_seq()
        return "\n".join(L)

    # -- batch ----------------------------------------------------------------
    def ops(self) -> dict[str, str]:
        out = {}
        for name, fn in inspect.getmembers(GameState, inspect.iscoroutinefunction):
            if not name.startswith("_"):
                out[name] = str(inspect.signature(fn)).replace("self, ", "").replace("(self)", "()")
        for name in BLOCKED_OPS:
            out.pop(name, None)
        return {**self.EXTRA_OPS, **out}

    # -- generic actions (actions.py) -------------------------------------------
    async def unit_actions(self, unit_id: int, limit: int = 30) -> dict:
        await self.ensure()
        return A.parse_listing(await self.conn.execute_write(A.unit_list_lua(unit_id, limit)))

    async def unit_act(self, unit_id: int, action: str, x: int | None = None, y: int | None = None,
                       params: dict | None = None) -> dict:
        await self.ensure()
        if not action.upper().startswith(("UNITOPERATION_", "UNITCOMMAND_")):
            action = "UNITOPERATION_" + action
        waited = action.upper() in WAITED_ACTIONS
        aimed = waited and x is not None and y is not None
        if waited:
            st = dict(kv.split("=", 1) for kv in (await self.conn.execute_write(STATUS_LUA))[0].split("|"))
            if st.get("blocking") == "ENDTURN_BLOCKING_CONSIDER_RAZE_CITY":
                # the engine holds every order until the keep/raze choice is made, then runs them
                return {"ok": False, "status": "REFUSED", "detail": "a captured city waits for keep/raze: call resolve_city_capture first"}
        before = (await self.conn.execute_write(A.unit_state_lua(unit_id)))[0]
        tgt_before = await self._target_state(x, y) if aimed else None
        busy = self._anim_busy()
        mark = self.last_seq()
        r = await self.conn.execute_write(A.unit_act_lua(unit_id, action, x, y, params))
        line = r[-1] if r else "ERR|no output"
        status, _, detail = line.partition("|")
        out = {"ok": status == "REQUESTED", "status": status, "detail": detail, "before": before}
        if not out["ok"]:
            return out
        if not waited:
            await asyncio.sleep(0.06)  # effects are readable one game-core publish later (~34 ms)
            out["after"] = (await self.conn.execute_write(A.unit_state_lua(unit_id)))[0]
            return out
        # A plain move shows in game core within ~0.1 s, but a combat is written only when its
        # animation ends (~10 s), and an order queued behind other animations starts up to 3.2 s
        # late. So: started = core state changed or this unit sent a signal; give up after 1 s
        # when no animation was playing, else after 6 s (all measured).
        after, tgt_after, started = before, tgt_before, False
        deadline = time.monotonic() + (6.0 if busy else 1.0)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.12)
            if any(self._signal_of(e, unit_id) for e in self.events_since(mark, signals=True)):
                started = True
                break
            after = (await self.conn.execute_write(A.unit_state_lua(unit_id)))[0]
            tgt_after = await self._target_state(x, y) if aimed else None
            if after != before or tgt_after != tgt_before:
                started = True
                break
        out["after"] = after
        if aimed:
            out["target_before"], out["target_after"] = tgt_before, tgt_after
        if not started:
            # the engine accepted the request (CanStart* passed) but dropped it
            out.update(ok=False, status="IGNORED", waited="nothing_happened",
                       detail=f"{detail}: accepted but nothing happened (unreachable target?)")
            return out
        # The outcome is settled; wait for the animation so later orders do not pile up behind
        # it. A changed target means a fight: wait for its end, not just the approach move.
        fought = aimed and tgt_before not in ("empty", "mine", "fog")
        out["waited"] = await self._wait_order_done(unit_id, mark, fought)
        out["after"] = (await self.conn.execute_write(A.unit_state_lua(unit_id)))[0]
        if aimed:
            out["target_after"] = await self._target_state(x, y)
            out["result"] = self._outcome(tgt_before, out["target_after"], out["after"])
        return out

    @staticmethod
    def _signal_of(e: dict, unit_id: int) -> bool:
        # my_moves_changed alone is no proof: auras and promotions change moves too
        return e["kind"] in ("combat_begin", "move_done") and unit_id in (e.get("id"), e.get("att_id"), e.get("def_id"))

    def _anim_busy(self) -> bool:
        """An animation of mine may still be playing: a combat without its end, or any signal
        in the last 2 s (a move animation sends nothing between its start and its end)."""
        recent = [e for e in self.events[-300:] if e["kind"] in SIGNALS]
        if recent and recent[-1].get("t", 0) > time.time() - 2.0:
            return True
        open_ = set()
        for e in recent:
            key = (e.get("att_id"), e.get("def_id"))
            if e["kind"] == "combat_begin":
                open_.add(key)
            elif e["kind"] == "combat_end":
                open_.discard(key)
        return bool(open_)

    async def _target_state(self, x: int, y: int) -> str:
        return (await self.conn.execute_write(A.target_state_lua(x, y)))[0]

    @staticmethod
    def _outcome(tb: str, ta: str, after: str) -> str:
        """One readable line: what the order did to the target plot."""
        if tb == ta:
            return "moved" if tb in ("empty", "mine", "fog") else "no effect on target"
        if ta in ("empty", "fog") or (ta == "mine" and tb != "mine"):
            return f"{tb} -> {ta} (target gone: killed, captured or taken)"
        return f"{tb} -> {ta}"

    async def _wait_order_done(self, unit_id: int, mark: int, fought: bool = False) -> str:
        """Wait for the animation of one order to finish instead of sleeping a fixed time.

        Signals come from runtime.py: my_moves_changed (order took effect), move_done
        (UnitMoveComplete), combat_begin/combat_end (CombatVisBegin/End). A melee attack
        through MOVE_TO is move -> combat -> (advance into the tile) move.
        """
        def mine(e: dict) -> bool:
            return unit_id in (e.get("id"), e.get("att_id"), e.get("def_id"))  # parse_event gives ints

        async def next_signal(kinds: set[str], after: int, timeout: float) -> dict | None:
            deadline = time.monotonic() + timeout
            while (left := deadline - time.monotonic()) > 0:
                e = await self.wait_event(kinds, after, left)
                if e is None:
                    return None
                if mine(e):
                    return e
                after = e["seq"]
            return None

        # Only called once the core state changed. Animations play one after another: an order
        # sent right behind other units' moves started 3.2 s later in a measured batch.
        start = await next_signal({"my_moves_changed", "move_done", "combat_begin", "combat_end"}, mark, 6.0)
        if start is None:
            return "no_signal"  # runtime events missing (e.g. game restarted): check status runtime_ok
        seen, after, result = start["kind"], start["seq"], "done"
        while True:
            if seen == "combat_begin":
                e = await next_signal({"combat_end"}, after, 20.0)
                if e is None:
                    return "combat_timeout"
                result, seen, after = "combat_done", "combat_end", e["seq"]
                continue
            # moving, moved, or a combat just ended: something may follow (attack after the
            # approach, advance after a kill); stop once it goes quiet.
            quiet = 15.0 if seen == "my_moves_changed" else 0.8
            if fought and result != "combat_done":
                quiet = 8.0  # the core already fought; its combat animation is still coming
            e = await next_signal({"move_done", "combat_begin"}, after, quiet)
            if e is None:
                return result
            seen, after = e["kind"], e["seq"]
            if seen == "move_done" and result == "done":
                result = "move_done"

    async def scan(self, radius: int = 4) -> list[str]:
        """Whole turn picture in one call: my cities, units that can act with attack previews,
        visible enemies within `radius` of my units (fog respected)."""
        await self.ensure()
        return await self.conn.execute_write(A.scan_lua(radius))

    async def city_actions(self, city_id: int, limit: int = 30) -> dict:
        await self.ensure()
        return A.parse_listing(await self.conn.execute_write(A.city_list_lua(city_id, limit)))

    async def city_act(self, city_id: int, action: str, params: dict | None = None) -> dict:
        await self.ensure()
        r = await self.conn.execute_write(A.city_act_lua(city_id, action, params))
        line = r[-1] if r else "ERR|no output"
        status, _, detail = line.partition("|")
        return {"ok": status == "REQUESTED", "status": status, "detail": detail}

    COMBAT_LUA = """
local me = Game.GetLocalPlayer()
local a = Players[me]:GetUnits():FindID(%(uid)d)
if a == nil then print("ERR|no such unit") return end
local x, y = %(x)d, %(y)d
if not PlayersVisibility[me]:IsVisible(x, y) then print("ERR|target plot not visible") return end
local d
for _, u in ipairs(Units.GetUnitsInPlot(Map.GetPlot(x, y))) do
  if u:GetOwner() ~= me and (d == nil or u:GetCombat() > d:GetCombat()) then d = u end
end
if d == nil then print("ERR|no enemy unit on target plot") return end
local ctype = nil
if a:GetRangedCombat() > 0 or a:GetBombardCombat() > 0 then
  ctype = CombatTypes.RANGED
  if a:GetBombardCombat() > a:GetRangedCombat() then ctype = CombatTypes.BOMBARD end
end
local r = CombatManager.SimulateAttackVersus(a:GetComponentID(), d:GetComponentID(), ctype)
local A, D = r[CombatResultParameters.ATTACKER], r[CombatResultParameters.DEFENDER]
local function side(t) return t[CombatResultParameters.COMBAT_STRENGTH] .. "|" .. t[CombatResultParameters.STRENGTH_MODIFIER] .. "|" .. t[CombatResultParameters.DAMAGE_TO] .. "|" .. tostring(t[CombatResultParameters.FINAL_DAMAGE_TO]) .. "|" .. tostring(t[CombatResultParameters.MAX_HIT_POINTS]) end
print("SIM|" .. (ctype == nil and "MELEE" or "RANGED") .. "|" .. GameInfo.Units[d:GetType()].UnitType .. "|" .. side(A) .. "|" .. side(D)
  .. "|" .. (a:GetMaxDamage() - a:GetDamage()) .. "|" .. (d:GetMaxDamage() - d:GetDamage()))
"""

    async def combat_estimate(self, unit_id: int, target_x: int, target_y: int) -> Any:
        """Game's own preview (CombatManager.SimulateAttackVersus, as UnitPanel.lua), incl. all modifiers.
        civ6-mcp's estimator ignores modifiers (seen: predicted 24 damage, real 44 with Discipline)."""
        await self.ensure()
        lines = await self.conn.execute_write(self.COMBAT_LUA % {"uid": int(unit_id), "x": int(target_x), "y": int(target_y)})
        line = lines[-1] if lines else "ERR|no output"
        p = line.split("|")
        if p[0] != "SIM":
            return {"error": p[-1]}
        keys = ["strength", "modifier", "damage_taken", "final_damage", "max_hp"]
        att = dict(zip(keys, p[3:8]))
        dfn = dict(zip(keys, p[8:13]))
        att["hp_now"], dfn["hp_now"] = int(p[13]), int(p[14])
        out = {"type": p[1], "defender_unit": p[2], "attacker": att, "defender": dfn}
        out["attacker_hp_after"] = att["hp_now"] - int(att["damage_taken"])
        out["defender_hp_after"] = dfn["hp_now"] - int(dfn["damage_taken"])
        return out

    PROGRESS_LUA = """
local me = Game.GetLocalPlayer()
local isCivic = %(civic)s
local tbl = isCivic and GameInfo.Civics or GameInfo.Technologies
local o = isCivic and Players[me]:GetCulture() or Players[me]:GetTechs()
for _, n in ipairs(%(names)s) do
  local r = tbl[n]
  if r then
    local has = isCivic and o:HasCivic(r.Index) or (not isCivic and o:HasTech(r.Index))
    local can = isCivic and o:CanProgress(r.Index) or (not isCivic and o:CanResearch(r.Index))
    print(n .. "|" .. tostring(has) .. "|" .. tostring(can))
  end
end
"""

    async def set_progress(self, name: str, civic: bool) -> str:
        """set_civic / set_research that never picks an unreachable target.
        Seen in play: civ6-mcp set_civic(CIVIC_EARLY_EMPIRE) without Foreign Trade answered
        PROGRESSING, the engine kept ENDTURN_BLOCKING_CIVIC forever. Here an unreachable target
        is replaced by the first reachable missing prerequisite (rules DB prereq tables)."""
        from . import rules
        table, col, pcol = ("CivicPrereqs", "Civic", "PrereqCivic") if civic else ("TechnologyPrereqs", "Technology", "PrereqTech")
        # all prerequisites, nearest first (BFS)
        order, frontier = [name], [name]
        while frontier:
            rows = rules.query(f"SELECT {pcol} FROM {table} WHERE {col} IN ({','.join(repr(x) for x in frontier)})")["rows"]
            frontier = [r[0] for r in rows if r[0] not in order]
            order += frontier
        from .actions import lua_list
        lines = await self.conn.execute_write(self.PROGRESS_LUA % {"civic": "true" if civic else "false", "names": lua_list(order)})
        state = {l.split("|")[0]: (l.split("|")[1] == "true", l.split("|")[2] == "true") for l in lines}
        has, can = state.get(name, (False, False))
        if has:
            return f"ERROR|{name} already completed"
        pick = name if can else next((n for n in reversed(order) if state.get(n, (True, False)) == (False, True)), None)
        if pick is None:
            return f"ERROR|{name} not reachable and no prerequisite can start now"
        r = await (self.gs.set_civic(pick) if civic else self.gs.set_research(pick))
        return r if pick == name else f"{r}|PREREQ_FIRST: {name} needs {pick} first (path {' <- '.join(order)})"

    EXTRA_OPS = {
        "combat_estimate": "(unit_id, target_x, target_y) -> expected damage both ways, modifiers",
        "unit_actions": "(unit_id, limit=30) -> legal actions of a unit, with targets / blocked_because",
        "unit_act": "(unit_id, action, x=None, y=None, params={PARAM_NAME: value}) -> REQUESTED|REFUSED",
        "city_actions": "(city_id, limit=30) -> city commands with targets, build queue",
        "city_act": "(city_id, action, params={PARAM_NAME: value}) e.g. BUILD/PURCHASE/RANGE_ATTACK/SET_FOCUS/MANAGE",
        "scan": "(radius=4) -> one-call turn picture: my cities, units that can act + attack previews, nearby enemies",
    }

    async def call(self, op: str, args: dict) -> Any:
        await self.ensure()
        if op in self.EXTRA_OPS:
            return jsonable(await getattr(self, op)(**args))
        if op == "set_civic":
            return await self.set_progress(args.get("civic_name") or args["tech_or_civic"], True)
        if op == "set_research":
            return await self.set_progress(args.get("tech_name") or args["tech_or_civic"], False)
        if op in BLOCKED_OPS:
            raise ValueError(f"op {op!r} is blocked: {BLOCKED_OPS[op]}")
        fn = getattr(self.gs, op, None)
        if op.startswith("_") or fn is None or not inspect.iscoroutinefunction(fn):
            raise ValueError(f"unknown op {op!r}; GET /v1/ops lists them")
        params = inspect.signature(fn).parameters
        args = dict(args)
        if "unit_id" in args and "unit_index" in params and "unit_id" not in params:
            args["unit_index"] = int(args.pop("unit_id")) % 65536
        for name, prm in params.items():  # JSON object keys are strings; restore int keys
            if name in args and isinstance(args[name], dict) and "dict[int" in str(prm.annotation):
                args[name] = {int(k): v for k, v in args[name].items()}
        return jsonable(await fn(**args))

    async def batch(self, ops: list[dict], stop_on_error: bool = True) -> list[dict]:
        results = []
        for spec in ops:
            spec = dict(spec)
            op = spec.pop("op")
            t0 = time.perf_counter()
            try:
                r = await self.call(op, spec)
                bad = (isinstance(r, str) and (BAD_RESULT.search(r) is not None or "SILENT_FAILURE" in r)) \
                    or (isinstance(r, dict) and r.get("ok") is False)
                results.append({"op": op, "ok": not bad, "result": r, "ms": round((time.perf_counter() - t0) * 1000)})
            except Exception as e:  # noqa: BLE001
                bad = True
                results.append({"op": op, "ok": False, "error": f"{type(e).__name__}: {e}"})
            if bad and stop_on_error:
                break
        return results

    # -- turns ----------------------------------------------------------------
    async def end_turn(self, timeout: float = 600) -> dict:
        await self.ensure()
        st = await self.status()
        if st.get("blocking") not in (None, "NONE"):
            return {"advanced": False, "reason": "blocked", "blocking": st["blocking"], "status": st}
        mark = self.last_seq()
        t0 = time.monotonic()
        turn0 = int(st.get("turn", 0))
        began = None
        for attempt in range(4):
            if st.get("turn_active") == "false" and attempt == 0:
                # Already ended (e.g. we stopped for a diplomacy session mid AI turn): just wait.
                ended = {"seq": mark}
                turn0 -= 1  # turn counter has not moved yet; any my_turn is progress
            else:
                await self.conn.execute_write("UI.RequestAction(ActionTypes.ACTION_ENDTURN)")
                ended = await self.wait_event({"turn_end"}, self.last_seq() if attempt else mark, 5)
            if ended is None:
                st = await self.status()
                sessions = jsonable(await self.gs.get_diplomacy_sessions())
                return {"advanced": False, "reason": "end turn not accepted", "status": st,
                        "diplomacy_sessions": sessions, "events": self.events_since(mark)}
            began = await self._wait_my_turn(ended["seq"], timeout)
            if began is not None and began.get("kind") == "diplomacy":
                return {"advanced": False, "reason": "diplomacy session waiting (answer with diplomacy_respond, "
                        "then advance again)", "diplomacy_sessions": began["sessions"], "events": self.events_since(mark)}
            if began is None:
                st = await self.status()
                return {"advanced": False, "reason": f"no my_turn after {timeout}s (AI hang or popup?)",
                        "status": st, "events": self.events_since(mark)}
            if began["turn"] > turn0:
                break
            # Same turn handed back (seen on first contact): retry unless a session waits for us.
            await asyncio.sleep(0.3)
            sessions = jsonable(await self.gs.get_diplomacy_sessions())
            if sessions and self.auto_ack and all(self._is_statement(x) for x in sessions):
                await self._ack(sessions)
                sessions = []
            if sessions:
                return {"advanced": False, "reason": "diplomacy session waiting", "diplomacy_sessions": sessions,
                        "status": await self.status(), "events": self.events_since(mark)}
        else:
            return {"advanced": False, "reason": "turn handed back 4 times", "status": await self.status(),
                    "events": self.events_since(mark)}
        # Turn-start events (production done, notifications...) arrive just after my_turn.
        settle_end = time.monotonic() + 1.5
        while time.monotonic() < settle_end:
            n = len(self.events)
            await asyncio.sleep(0.2)
            if len(self.events) == n:
                break
        return {"advanced": True, "turn": began["turn"], "seconds": round(time.monotonic() - t0, 2),
                "events": [e for e in self.events_since(mark) if e["kind"] not in ("my_turn", "turn_end")]}

    async def _wait_my_turn(self, after_seq: int, timeout: float) -> dict | None:
        """Wait for my_turn; every 4 s without it, close blocking popups.

        Seen in play: a "Natural Wonder Discovered" reveal (input context Reveal,
        cinematic camera) holds the turn until it is closed.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ev = await self.wait_event({"my_turn"}, after_seq, min(4.0, deadline - time.monotonic()))
            if ev is not None:
                return ev
            try:
                st = await self.status()
                if st.get("popup") == "true" or st.get("input") == "Reveal":
                    r = await self.gs.dismiss_popup()
                    self.popups_dismissed.append({"t": round(time.time(), 1), "turn": st.get("turn"), "result": r})
                    log.info("dismissed popup while waiting: %s", r)
                sessions = jsonable(await self.gs.get_diplomacy_sessions())
                if sessions and self.auto_ack and all(self._is_statement(x) for x in sessions):
                    await self._ack(sessions)
                elif sessions:  # a deal / choice / war: Claude decides
                    return {"kind": "diplomacy", "sessions": sessions}
            except Exception:  # noqa: BLE001
                log.exception("popup check failed")
        return None

    @staticmethod
    def _is_statement(sess: dict) -> bool:
        """AI remark with nothing to decide: no deal, no choices, not war."""
        return not sess.get("deal_summary") and not sess.get("choices") and not sess.get("is_at_war")

    async def _ack(self, sessions: list[dict]) -> None:
        for sess in sessions:
            pid = sess["other_player_id"]
            for _ in range(3):
                r = await self.gs.diplomacy_respond(pid, "POSITIVE")
                if "SESSION_CONTINUES" not in r:
                    break
            self.diplo_acked.append({"t": round(time.time(), 1), "from": sess.get("other_civ_name"),
                                     "text": sess.get("dialogue_text", "")[:200]})
            log.info("auto-acknowledged diplomacy from %s", sess.get("other_civ_name"))

    async def advance(self, turns: int = 1, stop: list[str] | None = None, timeout: float = 600,
                      auto_ack_diplomacy: bool = True) -> dict:
        self.auto_ack = auto_ack_diplomacy
        stop_kinds = set(stop if stop is not None else DEFAULT_STOP)
        done, log_ = 0, []
        mark0 = self.last_seq()
        t_start = time.time()
        reason = "turns done"
        for _ in range(turns):
            r = await self.end_turn(timeout)
            if not r["advanced"]:
                reason = r.get("reason", "?") + (f" ({r.get('blocking')})" if r.get("blocking") else "")
                log_.append(r)
                break
            done += 1
            hits = [e for e in r["events"] if e["kind"] in stop_kinds or (
                e["kind"] == "notification" and any(s in str(e.get("type", "")) for s in STOP_NOTIFICATIONS)
                and "city-state" not in str(e.get("msg", "")))]  # meeting a city-state needs no decision
            log_.append({"turn": r["turn"], "seconds": r["seconds"], "events": len(r["events"])})
            if hits:
                reason = "stop events: " + ", ".join(sorted({h["kind"] if h["kind"] != "notification" else h.get("type", "")
                                                            for h in hits}))
                break
            st = await self.status()
            if st.get("blocking") not in (None, "NONE"):
                reason = f"blocked: {st['blocking']}"
                break
        return {"turns_advanced": done, "stopped_because": reason, "per_turn": log_,
                "popups_dismissed": [p for p in self.popups_dismissed if p["t"] >= t_start],
                "diplomacy_acknowledged": [d for d in self.diplo_acked if d["t"] >= t_start],
                "events": self.events_since(mark0)[-200:]}

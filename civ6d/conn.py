"""One persistent FireTuner connection with a background reader.

Drop-in for civ6-mcp's GameConnection (execute_read / execute_write /
execute_in_state / lua_states / gamecore_index / ingame_index) without its fixed
0.1 s + 0.2 s drains: every request is terminated by a numbered marker, so a call
costs one round trip (~18 ms). Lines printed by in-game event handlers
("@@EV|...") arrive unsolicited and are handed to `on_event`.

Measured quirks (docs/01-baseline.md):
- after a client disconnects the tuner takes a few seconds to listen again;
- the game rebuilds the tuner on every save load; keep the socket quiet then.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from civ_mcp import tuner_client as tc
from civ_mcp.connection import LuaError
from civ_mcp.lua._helpers import SENTINEL

log = logging.getLogger("civ6d.conn")

END = "@@END@@"
PUSH = "@@EV|"


def _text(payload: str) -> str | None:
    """Value of a print() message ("O\\x00<ctx>: <value>"), None for other tags."""
    if not payload.startswith("O"):
        return None
    sep = payload.find(": ", 2)
    return payload[sep + 2:] if sep >= 0 else payload[1:].lstrip("\x00 ")


class FastConn:
    def __init__(self, host: str = "127.0.0.1", port: int = 4318,
                 on_event: Callable[[str], None] | None = None):
        self.host, self.port = host, port
        self.on_event = on_event or (lambda _t: None)
        self.lua_states: dict[int, str] = {}
        self.gamecore_index: int | None = None
        self.ingame_index: int | None = None
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._pending: tuple[int, list[str], asyncio.Future] | None = None
        self.connected_at: float | None = None

    # -- connection ---------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._w is not None and not self._w.is_closing() and (
            self._reader_task is not None and not self._reader_task.done())

    def state_index(self, name: str) -> int | None:
        return next((i for i, n in self.lua_states.items() if n == name), None)

    async def connect(self, attempts: int = 40) -> None:
        await self.disconnect()
        last: Exception | None = None
        for _ in range(attempts):
            try:
                self._r, self._w = await tc.connect(self.host, self.port)
                break
            except OSError as e:  # tuner re-listens a few seconds after a disconnect
                last = e
                await asyncio.sleep(0.5)
        else:
            raise ConnectionError(f"tuner {self.host}:{self.port} not reachable: {last}")
        _, raw = await tc.handshake(self._r, self._w)
        self.lua_states = {}
        for i in range(0, len(raw) - 1, 2):
            try:
                self.lua_states.setdefault(int(raw[i]), raw[i + 1])
            except ValueError:
                continue
        self.gamecore_index = self.state_index("GameCore_Tuner")
        self.ingame_index = self.state_index("InGame")
        self.connected_at = time.monotonic()
        self._reader_task = asyncio.create_task(self._reader())
        log.info("connected: %d Lua states, in_game=%s", len(self.lua_states), self.ingame_index is not None)

    async def disconnect(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._w:
            try:
                self._w.close()
                await self._w.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._r = self._w = None
        self._fail_pending(ConnectionError("disconnected"))

    async def ensure_connected(self) -> None:
        if not self.is_connected:
            await self.connect()

    async def reconnect(self) -> None:
        await self.connect()

    def _fail_pending(self, exc: Exception) -> None:
        if self._pending and not self._pending[2].done():
            self._pending[2].set_exception(exc)
        self._pending = None

    async def _reader(self) -> None:
        assert self._r is not None
        try:
            while True:
                msg = await tc.recv_message(self._r)
                p = msg.payload
                if p.startswith("ERR:"):
                    if self._pending and not self._pending[2].done():
                        self._pending[2].set_exception(LuaError(p))
                    continue
                text = _text(p)
                if text is None:
                    continue
                if text.startswith(PUSH):
                    try:
                        self.on_event(text)
                    except Exception:  # noqa: BLE001
                        log.exception("on_event failed")
                    continue
                if not self._pending:
                    continue
                seq, lines, fut = self._pending
                if text.startswith(END):
                    if text == f"{END}{seq}" and not fut.done():
                        fut.set_result(lines)
                    continue
                if text.strip() != SENTINEL:
                    lines.append(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  (socket closed by game / load)
            log.warning("reader stopped: %r", e)
            self._fail_pending(ConnectionError(f"tuner connection lost: {e!r}"))
            if self._w:
                self._w.close()

    # -- execution ----------------------------------------------------------
    async def execute_in_state(self, state_index: int, lua_code: str, timeout: float = 10.0) -> list[str]:
        async with self._lock:
            await self.ensure_connected()
            assert self._w is not None
            self._seq += 1
            seq = self._seq
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending = (seq, [], fut)
            # Wrapped in a function so an early top-level `return` still reaches the
            # end marker (a bare do...end block would return from the whole chunk).
            await tc.send_message(self._w, tc.TAG_COMMAND,
                                  f"CMD:{state_index}:(function()\n{lua_code}\nend)()\nprint('{END}{seq}')")
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                partial = self._pending[1] if self._pending and self._pending[0] == seq else []
                self._pending = None
                log.warning("Lua timeout after %.1fs (%d lines)", timeout, len(partial))
                return partial
            finally:
                if self._pending and self._pending[0] == seq:
                    self._pending = None

    async def _need(self, attr: str) -> int:
        await self.ensure_connected()
        idx = getattr(self, attr)
        if idx is None:  # connected at the menu: states appear only after a load
            await self.connect()
            idx = getattr(self, attr)
        if idx is None:
            raise ConnectionError("no game loaded (GameCore_Tuner/InGame Lua states missing)")
        return idx

    async def execute_read(self, lua_code: str, timeout: float = 10.0) -> list[str]:
        return await self.execute_in_state(await self._need("gamecore_index"), lua_code, timeout)

    async def execute_write(self, lua_code: str, timeout: float = 10.0) -> list[str]:
        return await self.execute_in_state(await self._need("ingame_index"), lua_code, timeout)

    async def run(self, state: str, lua_code: str, timeout: float = 10.0) -> list[str]:
        idx = self.state_index(state) if self.lua_states else None
        if idx is None:
            await self.ensure_connected()
            idx = self.state_index(state)
        if idx is None:
            raise ConnectionError(f"Lua state {state!r} not available")
        return await self.execute_in_state(idx, lua_code, timeout)

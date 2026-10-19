"""Thin FireTuner helper for probing Civ VI (reuses civ6-mcp's wire client).

Usage as module:  async with Tuner() as t: lines = await t.run("InGame", "print(1)")
Usage as CLI:     uv run --project civ6-mcp python tools/tuner.py <state> '<lua>'
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "civ6-mcp" / "src"))
from civ_mcp import tuner_client as tc  # noqa: E402

END = "@@END@@"
PUSH = "@@EV|"  # prefix of lines printed by in-game event handlers


class Tuner:
    def __init__(self, host: str = "127.0.0.1", port: int = 4318):
        self.host, self.port = host, port
        self.states: dict[str, int] = {}
        self.pushed: list[tuple[float, str]] = []  # unsolicited output seen

    async def __aenter__(self) -> "Tuner":
        # Tuner re-listens a few seconds after a client disconnects: retry.
        for attempt in range(30):
            try:
                self.r, self.w = await tc.connect(self.host, self.port)
                break
            except OSError:
                if attempt == 29:
                    raise
                await asyncio.sleep(0.5)
        _, raw = await tc.handshake(self.r, self.w)
        self.adopt_states(raw)
        return self

    @classmethod
    def from_streams(cls, reader, writer, raw_states: dict[int, str]) -> "Tuner":
        t = cls()
        t.r, t.w = reader, writer
        t.states = {name: i for i, name in raw_states.items()}
        return t

    def adopt_states(self, raw: list[str]) -> None:
        for i in range(0, len(raw) - 1, 2):
            try:
                self.states.setdefault(raw[i + 1], int(raw[i]))
            except ValueError:
                pass

    async def __aexit__(self, *exc) -> None:
        self.w.close()
        await self.w.wait_closed()

    def idx(self, state: str | int) -> int:
        return state if isinstance(state, int) else self.states[state]

    async def run(self, state: str | int, code: str, timeout: float = 10.0) -> list[str]:
        """Send Lua, collect printed lines until END sentinel. No fixed drains."""
        await tc.send_message(self.w, tc.TAG_COMMAND, f"CMD:{self.idx(state)}:{code}\nprint('{END}')")
        out: list[str] = []
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            msg = await tc.recv_message_timeout(self.r, timeout=left)
            if msg is None:
                break
            p = msg.payload
            if p.startswith("ERR:"):
                raise RuntimeError(p)
            if not p.startswith("O"):
                continue
            text = p[p.find(": ", 2) + 2:] if ": " in p[:80] else p[1:].lstrip("\x00 ")
            if text.strip() == END:
                return out
            if text.startswith(PUSH):
                self.pushed.append((time.monotonic(), text))
                continue
            out.append(text)
        raise TimeoutError(f"no {END} after {timeout}s; got {out[-5:]}")

    async def listen(self, seconds: float) -> list[tuple[float, str]]:
        """Collect unsolicited messages for a while (push channel test)."""
        got = []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            msg = await tc.recv_message_timeout(self.r, timeout=left)
            if msg is None:
                break
            p = msg.payload
            text = p[p.find(": ", 2) + 2:] if ": " in p[:80] else p
            got.append((time.monotonic(), text))
            if text.startswith(PUSH):
                self.pushed.append((time.monotonic(), text))
        return got

    async def wait_push(self, needle: str, timeout: float) -> tuple[float, str] | None:
        """Block until a pushed event containing needle arrives (no polling)."""
        start = len(self.pushed)
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            for ev in self.pushed[start:]:
                if needle in ev[1]:
                    return ev
            start = len(self.pushed)
            await self.listen(min(left, 0.05))
        return None


async def _main() -> None:
    state, code = sys.argv[1], sys.argv[2]
    async with Tuner() as t:
        if state == "states":
            print(t.states)
            return
        t0 = time.perf_counter()
        lines = await t.run(int(state) if state.isdigit() else state, code)
        print("\n".join(lines))
        print(f"[{(time.perf_counter() - t0) * 1000:.0f} ms]", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(_main())

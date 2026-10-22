"""Five-minute HackRF reset scheduler, independent of signal generation."""
from __future__ import annotations

import asyncio
import time
from typing import Callable


class RadioMaintenance:
    def __init__(self, engine, reset, *, clock: Callable = time.monotonic):
        self.engine, self.reset, self.clock = engine, reset, clock
        self.enabled = True
        self.interval_s = 300
        self.interrupt = False
        self.due = self.clock() + self.interval_s
        self.lock = asyncio.Lock()
        self.last_result = None
        self.status = "scheduled"
        engine.maintenance_status = self.snapshot

    def snapshot(self):
        return {"enabled": self.enabled, "interval_s": self.interval_s,
                "interrupt": self.interrupt, "status": self.status,
                "next_in_s": max(0, int(self.due-self.clock())) if self.enabled else None,
                "last_result": self.last_result}

    def configure(self, *, enabled: bool, interrupt: bool):
        self.enabled, self.interrupt = enabled, interrupt
        self.due = self.clock() + self.interval_s
        self.status = "scheduled" if enabled else "disabled"
        return self.snapshot()

    async def tick(self):
        if not self.enabled or self.clock() < self.due or self.lock.locked():
            return
        if self.engine.state != "idle" and not self.interrupt:
            if self.status != "waiting for idle":
                self.engine.logs.put_nowait("# five-minute reset due; waiting for idle to preserve continuous GPS time")
            self.status = "waiting for idle"
            return
        try:
            await self.reset_now(force=self.interrupt)
        except (RuntimeError, OSError) as exc:
            self.engine.logs.put_nowait(f"!! scheduled HackRF reset failed: {exc}")

    async def reset_now(self, *, force=False):
        if self.lock.locked():
            raise RuntimeError("a HackRF reset is already running")
        async with self.lock:
            if self.engine.state != "idle":
                if not force:
                    raise RuntimeError("stop the current session before resetting HackRF")
                proc = self.engine._proc
                await self.engine.stop(reason="five-minute HackRF reset (interrupt mode)")
                if proc is not None and proc.returncode is None:
                    raise RuntimeError("transfer process did not exit; reset blocked")
            self.engine.state = "resetting"
            self.status = "resetting"
            self.engine.logs.put_nowait("# resetting HackRF through libhackrf; no firmware is being flashed")
            try:
                result = await self.reset()
                self.last_result = {"ok": True, "at": time.time(), **result}
                self.status = "scheduled" if self.enabled else "disabled"
                self.engine.logs.put_nowait(f"+ HackRF reset verified: {result['serial']}")
                return self.last_result
            except Exception as exc:
                self.status = "reset failed"
                self.last_result = {"ok": False, "at": time.time(), "error": str(exc)}
                raise RuntimeError(str(exc)) from exc
            finally:
                self.due = self.clock() + self.interval_s
                self.engine.state = "idle"

    async def run(self):
        while True:
            await asyncio.sleep(1)
            await self.tick()

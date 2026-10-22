import asyncio
import unittest
from types import SimpleNamespace

from radio_maintenance import RadioMaintenance


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0
        self.events = []
        async def stop(**kwargs):
            self.events.append("stop")
            self.engine.state = "idle"
        self.engine = SimpleNamespace(state="idle", logs=asyncio.Queue(), _proc=None, stop=stop)
        async def reset():
            self.assertEqual(self.engine.state, "resetting")
            self.events.append("reset")
            return {"serial": "0"*32, "verified": True}
        self.timer = RadioMaintenance(self.engine, reset, clock=lambda: self.now)

    async def test_five_minute_interval_and_rearm(self):
        self.now = 299
        await self.timer.tick()
        self.assertEqual(self.events, [])
        self.now = 300
        await self.timer.tick()
        self.assertEqual(self.events, ["reset"])
        self.assertEqual(self.timer.due, 600)
        self.assertTrue(self.timer.last_result["verified"])

    async def test_active_session_is_deferred_by_default(self):
        self.now = 300
        self.engine.state = "transmitting"
        await self.timer.tick()
        self.assertEqual(self.timer.status, "waiting for idle")
        self.assertEqual(self.events, [])
        self.engine.state = "idle"
        await self.timer.tick()
        self.assertEqual(self.events, ["reset"])

    async def test_interrupt_mode_stops_before_reset_without_restart(self):
        self.timer.configure(enabled=True, interrupt=True)
        self.now = 300
        self.engine.state = "transmitting"
        await self.timer.tick()
        self.assertEqual(self.events, ["stop", "reset"])
        self.assertEqual(self.engine.state, "idle")

    async def test_reset_failure_is_reported_and_not_retried_each_tick(self):
        async def fail():
            self.events.append("attempt")
            raise RuntimeError("USB unavailable")
        self.timer.reset = fail
        self.now = 300
        await self.timer.tick()
        await self.timer.tick()
        self.assertEqual(self.events, ["attempt"])
        self.assertFalse(self.timer.last_result["ok"])
        self.assertEqual(self.engine.state, "idle")

    async def test_manual_reset_refuses_active_session(self):
        self.engine.state = "generating"
        with self.assertRaisesRegex(RuntimeError, "stop the current session"):
            await self.timer.reset_now()
        self.assertEqual(self.events, [])
        self.assertEqual(self.engine.state, "generating")

    async def test_disabled_timer_does_nothing(self):
        self.timer.configure(enabled=False, interrupt=False)
        self.now = 1000
        await self.timer.tick()
        self.assertEqual(self.events, [])

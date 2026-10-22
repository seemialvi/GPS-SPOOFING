import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from spoof_engine import SpoofEngine


@unittest.skipUnless(os.name == "nt", "Windows console behavior")
class WindowsTransferStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_delivers_sigint_instead_of_force_killing(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = SpoofEngine(Path(temp))
            engine.state = "transmitting"
            script = ("import signal,sys,time; "
                      "signal.signal(signal.SIGINT,lambda *_:sys.exit(0)); "
                      "print('ready',flush=True); time.sleep(60)")
            task = asyncio.create_task(engine._run([sys.executable,"-c",script], "transfer"))
            engine._task = task
            self.assertEqual(await asyncio.wait_for(engine.logs.get(),timeout=5), "ready")
            proc = engine._proc
            try:
                await engine.stop()
                self.assertEqual(proc.returncode, 0, "child must handle SIGINT and exit cleanly")
                self.assertEqual(await task, 0)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

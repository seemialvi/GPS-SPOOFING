"""Native encoder and file-generation integration checks; never uses a radio."""
from __future__ import annotations

import re
import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rinex_snapshot import prepare_snapshot
from spoof_engine import Scenario, SpoofEngine, build_generate_cmd

FIXTURE = ROOT / "tests/fixtures/broadcast.26n"
TAPS = (
    (1,2,3,5,6,10,11,12,13,14,17,18,20,23),
    (2,3,4,6,7,11,12,13,14,15,18,19,21,24),
    (1,3,4,5,7,8,12,13,14,15,16,19,20,22),
    (2,4,5,6,8,9,13,14,15,16,17,20,21,23),
    (1,3,5,6,7,9,10,14,15,16,17,18,21,22,24),
    (3,5,6,8,9,10,11,13,15,19,22,23,24),
)
IDS4 = [57,25,26,27,28,57,29,30,31,32,57,62,52,53,54,57,55,56,58,59,57,60,61,62,63]


def decode(word, previous):
    """Independent bit-by-bit ICD parity equations, including D30 inversion."""
    payload = word >> 6
    if previous & 1:
        payload ^= 0xFFFFFF
    carries = [(previous >> 1)&1, previous&1, (previous>>1)&1,
               previous&1, previous&1, (previous>>1)&1]
    for i, taps in enumerate(TAPS):
        parity = carries[i]
        for bit in taps:
            parity ^= (payload >> (24-bit)) & 1
        if parity != ((word >> (5-i)) & 1):
            raise AssertionError(f"invalid parity bit {25+i}")
    return payload


class SixSatelliteTests(unittest.TestCase):
    def test_cached_selection_of_bad_header_uses_verified_file(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            good = FIXTURE.read_text()
            bad = "\n".join(
                f"{'   1.860000000000D-09-7.600000000000D-09   518400        0':60}DELTA-UTC: A0,A1,T,W"
                if "DELTA-UTC" in line else line for line in good.splitlines()
            ) + "\n"
            (folder/"brdc2860.26n").write_text(bad)
            (folder/"brdc2880.26n").write_text(good)
            with self.assertRaisesRegex(ValueError, "multiple of 4096"):
                prepare_snapshot(folder/"brdc2860.26n")

            async def run():
                engine = SpoofEngine(folder)
                engine.scenario.duration_s = 2
                self.assertEqual(engine.ephemeris_status()["files"], ["brdc2880.26n"])
                await engine.start(rf=False, ephemeris="brdc2860.26n")
                await engine._task
                self.assertEqual(engine.telemetry["ephemeris_source"], "brdc2880.26n")
                self.assertEqual(len(engine.telemetry.get("prns", [])), 6)
                self.assertEqual(engine.iq_path.stat().st_size, engine.scenario.est_bytes)
                logs = []
                while not engine.logs.empty():
                    logs.append(engine.logs.get_nowait())
                self.assertTrue(any("using verified brdc2880.26n" in line for line in logs))
                self.assertFalse(any(line.startswith("!!") for line in logs))
            asyncio.run(run())

    def test_complete_navigation_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            nav = Path(temp)/"snapshot.rnx"
            info = prepare_snapshot(FIXTURE, nav)
            self.assertGreaterEqual(info["records"], 6)
            result = subprocess.run([ROOT/"build/nav_probe.exe", nav],
                                    capture_output=True, text=True, check=True)
        rows = result.stdout.splitlines()
        self.assertEqual(len(rows), 26*6)
        ephemerides = {}
        pages = {}
        previous_frames = {}
        first_second = int(rows[0].split()[2])
        for row in rows:
            parts = row.split()
            frame, prn, second = map(int, parts[:3])
            words = [int(w,16) for w in parts[3:]]
            self.assertEqual(len(words), 60)
            if frame:
                self.assertEqual(words[:10], previous_frames[prn][-10:])
            previous_frames[prn] = words
            previous = 0
            decoded = []
            for word in words:
                decoded.append(decode(word, previous))
                previous = word
            payload = decoded[10:]
            for sf in range(5):
                self.assertEqual(payload[sf*10] >> 16, 0x8B)
                how = payload[sf*10+1]
                self.assertEqual((how >> 2)&7, sf+1)
                self.assertEqual((how >> 7)&0x1FFFF, (second//6+sf+1)%100800)
                self.assertEqual(words[10+sf*10+1]&3, 0)
                self.assertEqual(words[10+sf*10+9]&3, 0)
            page = (second//30)%25
            id4, id5 = (payload[32]>>16)&63, (payload[42]>>16)&63
            self.assertEqual(id4, IDS4[page] if not (25 <= IDS4[page] <= 32 and id4==0) else 0)
            self.assertIn(id5, (page+1 if page<24 else 51, 0))
            pages.setdefault(prn, set()).add(page)
            if frame==0:
                ephemerides[prn] = tuple(payload[12:30])
            if page==17:
                self.assertEqual(id4, 56)
            if page==24:
                self.assertEqual((payload[42]>>8)&255, (first_second//4096)&255)
        self.assertEqual(len(ephemerides), 6)
        self.assertEqual(len(set(ephemerides.values())), 6)
        self.assertTrue(all(len(seen)==25 for seen in pages.values()))

    def test_website_coordinates_reach_six_channel_generator(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            nav = folder/"snapshot.rnx"
            prepare_snapshot(FIXTURE, nav)
            ranges = []
            for lat, lon, start in [(-33.8688,151.2093,"2026/10/15,12:00:00"),
                                    (24.8607,67.0011,"2026/10/15,11:43:00")]:
                sc = Scenario(lat=lat, lon=lon, duration_s=2)
                out = folder/"signal.bin"
                cmd = build_generate_cmd(str(ROOT/"bin/gps-sdr-sim.exe"), sc, nav,
                                         out, start, overwrite_time=True)
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                self.assertEqual(out.stat().st_size, sc.est_bytes)
                channels = re.findall(r"LAB_CHANNELS ([\d,]+)", result.stderr)
                self.assertEqual(len(set(channels[0].split(','))), 6)
                self.assertIn(f"{lat:.6f}", result.stderr)
                self.assertIn(f"{lon:.6f}", result.stderr)
                ranges.append(re.findall(r"^\d{2} .+$", result.stderr, re.MULTILINE))
                self.assertEqual(len(ranges[-1]), 6)
            self.assertNotEqual(ranges[0], ranges[1])

    def test_converter_preserves_real_header(self):
        # A malformed old file had its UTC label shifted beyond column 61.
        with tempfile.TemporaryDirectory() as temp:
            nav = Path(temp)/"snapshot.rnx"
            prepare_snapshot(FIXTURE, nav)
            labels = {line[60:].strip(): line[:60] for line in nav.read_text().splitlines()[:8]}
            self.assertIn("DELTA-UTC: A0,A1,T,W", labels)
            self.assertEqual(int(labels["LEAP SECONDS"]), 18)


if __name__ == "__main__":
    unittest.main()

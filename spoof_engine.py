"""
Core engine: generates a GPS L1 C/A I/Q file with gps-sdr-sim and,
optionally, transmits it with hackrf_transfer.

This targets the osqzss/gps-sdr-sim command line, which is file-based
(see ETHERNET: the generator has no SDR output of its own):

    gps-sdr-sim   -e <rinex nav>  -l "lat,lon,height"  -o <iq.bin>
                  -s <sample rate>  -b <bits>  -t "<scenario time>"  -d <sec>

    hackrf_transfer -t <iq.bin> -f 1575420000 -s <rate> -x <gain dB>
                    -b <baseband bw> -a 1 -R

Separated from the web layer so you can drive it headless or test it
in isolation. Nothing here touches the radio directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rinex_snapshot import prepare_snapshot

# Defaults; the environment is read at call time so these can be
# overridden after import.
GENERATOR = "gps-sdr-sim"
TRANSFER = "hackrf_transfer"

# GPS L1 C/A. The NEO-6M is a 2007 single-band receiver - no L2, no L5 -
# so this is the only frequency that produces a fix on it.
L1_FREQ_HZ = 1_575_420_000
L1_BANDWIDTH_HZ = 2_046_000

# gps-sdr-sim's own defaults, and the closest hackrf_transfer baseband
# filter above the 2.046 MHz L1 main lobe.
DEFAULT_SAMPLE_RATE = 2_600_000
DEFAULT_BB_BW = 2_500_000
# Give the renderer a short head start, then begin RF when the generated GPS
# time reaches wall-clock UTC.  This avoids feeding a warm receiver a waveform
# that starts several seconds in the past.
AUTO_TIME_LEAD_S = 60
SATELLITE_COUNT = 6
NAV_CYCLE_S = 750


def _resolve(name: str) -> str | None:
    """Deprecated shim - use toolpaths.resolve."""
    from toolpaths import resolve

    return resolve(name)


def generator_path() -> str | None:
    from toolpaths import resolve

    return resolve("gps_sdr_sim")


def transfer_path() -> str | None:
    from toolpaths import resolve

    return resolve("hackrf_transfer")


def _tool_status() -> dict:
    from toolpaths import status

    try:
        return status()
    except Exception:
        return {}


# --------------------------------------------------------------- RINEX nav

# First data record: " 1 22  1  1  0  0  0.0 ..." = PRN, YY, MM, DD, hh, mm, ss
_EPOCH_RE = re.compile(
    r"^\s*\d+\s+(\d{2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})",
    re.MULTILINE,
)


def rinex_epoch(path: Path) -> str | None:
    """The scenario time (-t) to use for a RINEX nav file.

    Chooses the *modal* epoch rather than the first. gps-sdr-sim keeps
    ephemeris as eph[week][prn], so the newest record for each satellite
    is the one that survives, and it accepts a satellite only when its
    clock reference is within an hour of the scenario time. Broadcast
    files carry an hourly history plus stragglers from satellites that
    have stopped transmitting, so the most common epoch puts the most
    satellites in range; the first epoch is often the sparsest.
    """
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None

    epochs = _EPOCH_RE.findall(text)
    if not epochs:
        return None

    counts: dict[tuple, int] = {}
    for g in epochs:
        key = tuple(int(x) for x in g)
        counts[key] = counts.get(key, 0) + 1
    yy, mo, d, hh, mm, ss = max(counts.items(), key=lambda kv: kv[1])[0]
    year = 2000 + yy if yy < 80 else 1900 + yy
    return f"{year:04d}/{mo:02d}/{d:02d},{hh:02d}:{mm:02d}:{ss:02d}"


@dataclass
class Scenario:
    """Where the receiver should believe it is, and how to radiate it."""

    lat: float = -33.8688
    lon: float = 151.2093
    alt_m: float = 0.0

    # --- generation
    duration_s: int = NAV_CYCLE_S  # one complete 25-frame LNAV cycle
    sample_rate: int = DEFAULT_SAMPLE_RATE
    iq_bits: int = 8            # signed 8-bit I/Q for hackrf_transfer
    scenario_time: str | None = None  # None -> current UTC, with TOC/TOE rewritten

    # --- transmission
    freq_hz: int = L1_FREQ_HZ
    tx_gain_db: int = 0         # bench default; set from measured cable attenuation
    baseband_bw_hz: int = DEFAULT_BB_BW
    amp_enable: bool = False    # external attenuation determines receiver input power
    repeat: bool = False        # looping makes GPS time jump backwards
    serial: str | None = None   # pin to one radio: -d

    def check(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        if not -1000.0 <= self.alt_m <= 100000.0:
            raise ValueError("altitude outside sane range")
        if not 1 <= self.duration_s <= 86400:
            raise ValueError("duration must be 1-86400 s")
        if self.iq_bits not in (1, 8, 16):
            raise ValueError("iq_bits must be 1, 8 or 16")
        # hackrf_transfer supports 2-20 MS/s; the generator is happy wider,
        # but anything outside that would be rejected at transmit time.
        if not 2_000_000 <= self.sample_rate <= 20_000_000:
            raise ValueError("sample rate must be 2-20 MS/s (hackrf_transfer range)")
        # hackrf_transfer documents 1 MHz - 6 GHz without needing -F.
        if not 1_000_000 <= self.freq_hz <= 6_000_000_000:
            raise ValueError(
                f"{self.freq_hz/1e6:.3f} MHz is outside hackrf_transfer's "
                "1 MHz - 6 GHz range. The NEO-6M needs 1575.42 MHz."
            )
        if not 0 <= self.tx_gain_db <= 47:
            raise ValueError("TX gain must be 0-47 dB (hackrf_transfer -x range)")

    @property
    def l1_locked(self) -> bool:
        return abs(self.freq_hz - L1_FREQ_HZ) < 1_000_000

    @property
    def bytes_per_sample(self) -> int:
        # I and Q, each 8 or 16 bit
        return 2 if self.iq_bits == 8 else 4

    @property
    def est_bytes(self) -> int:
        if self.iq_bits == 1:
            return (self.sample_rate * self.duration_s * 2 + 7) // 8
        return int(self.sample_rate * self.duration_s * self.bytes_per_sample)


def build_generate_cmd(
    exe: str, sc: Scenario, rinex: Path, out: Path, when: str,
    *, overwrite_time: bool = False,
) -> list[str]:
    """gps-sdr-sim invocation for this fork's CLI."""
    return [
        exe,
        "-e", str(rinex),
        "-l", f"{sc.lat:.7f},{sc.lon:.7f},{sc.alt_m:.3f}",
        "-o", str(out),
        "-s", str(sc.sample_rate),
        "-b", str(sc.iq_bits),
        "-N", str(SATELLITE_COUNT),
        "-v",
        "-T" if overwrite_time else "-t", when,
        "-d", str(sc.duration_s),
    ]


def build_transfer_cmd(exe: str, sc: Scenario, iq: Path) -> list[str]:
    """hackrf_transfer invocation: replay the rendered I/Q on L1."""
    if sc.iq_bits != 8:
        raise ValueError("hackrf_transfer requires signed 8-bit I/Q; render again with iq_bits=8")
    cmd = [
        exe,
        "-t", str(iq),
        "-f", str(sc.freq_hz),
        "-s", str(sc.sample_rate),
        "-b", str(sc.baseband_bw_hz),
        "-x", str(sc.tx_gain_db),
        "-a", "1" if sc.amp_enable else "0",
    ]
    if sc.repeat:
        cmd.append("-R")
    if sc.serial:
        cmd += ["-d", sc.serial]
    return cmd


# ------------------------------------------------------------------- engine

_TIME_RE = re.compile(r"Time into run\s*=\s*([\d.]+)", re.IGNORECASE)
_SATS_RE = re.compile(r"^\s*(\d{2})\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s*$")


class SpoofEngine:
    def __init__(self, workdir: Path):
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.iq_path = self.workdir / "gpssim.bin"
        self.nav_path = self.workdir / "six_snapshot.rnx"

        self.logs: asyncio.Queue[str] = asyncio.Queue()
        self.telemetry: dict = {}
        self.progress = {"stage": "idle", "seconds": 0.0}

        self.scenario = Scenario()
        self.state = "idle"           # idle | generating | transmitting
        self.started_at: float = 0.0
        self.generator = generator_path()
        self.transfer = transfer_path()

        self._proc: asyncio.subprocess.Process | None = None
        self._proc_stage = None
        self._task: asyncio.Task | None = None
        self.on_idle = None  # type: ignore[assignment]
        self.maintenance_status = None

    # ---------------------------------------------------------------- helpers

    def rinex_files(self) -> list[str]:
        return sorted(p.name for p in self.workdir.iterdir()
                      if p.is_file() and re.fullmatch(r"\.\d{2}n|\.nav", p.suffix, re.IGNORECASE))

    def refresh_tools(self) -> None:
        self.generator = generator_path()
        self.transfer = transfer_path()

    def ephemeris_status(self) -> dict:
        valid, rejected = [], {}
        for name in self.rinex_files():
            try:
                prepare_snapshot(self.workdir / name)
                valid.append(name)
            except (ValueError, OSError) as exc:
                rejected[name] = str(exc)
        return {"files": valid, "rejected": rejected,
                "recommended": valid[-1] if valid else None}

    def scenario_time(self, rinex: str | None) -> str | None:
        if self.scenario.scenario_time:
            return self.scenario.scenario_time
        if self.state != "idle" and self.telemetry.get("scenario_time"):
            return str(self.telemetry["scenario_time"])
        if rinex:
            # gps-sdr-sim's calendar is GPS time, not UTC. Read the offset
            # from the chosen navigation file and align with a full frame.
            text = (self.workdir / rinex).read_text(errors="ignore")
            leap = re.search(r"^\s*(\d+)\s+.*LEAP SECONDS", text, re.MULTILINE)
            if not leap:
                return None
            # Rendering cost scales with duration and sample rate. Reserve
            # extra lead for a full cycle rather than scheduling it too early.
            lead = max(AUTO_TIME_LEAD_S,
                       30 + self.scenario.duration_s * self.scenario.sample_rate / DEFAULT_SAMPLE_RATE * 0.20)
            gps_seconds = time.time() + lead + int(leap.group(1))
            when = datetime.fromtimestamp(
                (int(gps_seconds)//30+1)*30, timezone.utc
            )
            return when.strftime("%Y/%m/%d,%H:%M:%S")
        return None

    @property
    def blocking(self) -> str | None:
        """What is preventing RF, if anything."""
        if self.generator is None:
            return "generator"
        if self.transfer is None:
            return "transfer"
        if not self.rinex_files():
            return "ephemeris"
        return None

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "maintenance": self.maintenance_status() if self.maintenance_status else None,
            "elapsed": (time.time() - self.started_at) if self.started_at else 0.0,
            "scenario": asdict(self.scenario),
            "ephemeris": self.rinex_files(),
            "progress": self.progress,
            "telemetry": self.telemetry,
            "navigation": {
                "satellite_count": SATELLITE_COUNT,
                "bit_rate": 50,
                "word_bits": 30,
                "subframe_bits": 300,
                "subframe_seconds": 6,
                "frame_seconds": 30,
                "cycle_frames": 25,
                "cycle_seconds": NAV_CYCLE_S,
            },
            "generator": self.generator,
            "transfer": self.transfer,
            "blocking": self.blocking,
            "tools": _tool_status(),
            "iq_path": str(self.iq_path),
            "iq_size": self.iq_path.stat().st_size if self.iq_path.is_file() else 0,
            "commands": {
                "generate": self._cmd_preview("generate"),
                "transfer": self._cmd_preview("transfer"),
            },
            "rf": {
                "l1_mhz": L1_FREQ_HZ / 1e6,
                "l1_bandwidth_mhz": L1_BANDWIDTH_HZ / 1e6,
                "l1_locked": self.scenario.l1_locked,
                "freq_mhz": self.scenario.freq_hz / 1e6,
                "sample_rate_msps": self.scenario.sample_rate / 1e6,
                "tx_gain_db": self.scenario.tx_gain_db,
                "baseband_bw_mhz": self.scenario.baseband_bw_hz / 1e6,
            },
        }

    def _cmd_preview(self, which: str) -> str:
        eph = self.rinex_files()
        when = self.scenario_time(eph[-1] if eph else None)
        exe = self.generator if which == "generate" else self.transfer
        if not exe:
            return f"# {which}: tool not found"
        if which == "generate":
            if not eph:
                return "# generate: no RINEX nav file in work/"
            return " ".join(
                build_generate_cmd(exe, self.scenario, self.nav_path,
                                   self.iq_path, when or "",
                                   overwrite_time=self.scenario.scenario_time is None)
            )
        if not self.iq_path.is_file():
            return "# transfer: render an I/Q file first"
        if self.scenario.iq_bits != 8:
            return "# transfer: requires signed 8-bit I/Q"
        return " ".join(build_transfer_cmd(exe, self.scenario, self.iq_path))

    # ------------------------------------------------------------- lifecycle

    async def start(self, *, rf: bool, ephemeris: str | None = None,
                    shielded: bool = False, serial: str | None = None) -> dict:
        if self.state != "idle":
            raise RuntimeError("a session is already running")

        self.refresh_tools()
        self.scenario.check()
        if rf and self.scenario.iq_bits != 8:
            self.logs.put_nowait(
                f"# RF mode requires signed 8-bit I/Q; overriding {self.scenario.iq_bits}-bit render"
            )
            self.scenario.iq_bits = 8
        if rf and self.scenario.duration_s < NAV_CYCLE_S:
            self.logs.put_nowait(
                f"# receiver acquisition needs continuous time; extending "
                f"{self.scenario.duration_s}s render to {NAV_CYCLE_S}s (25 frames)"
            )
            self.scenario.duration_s = NAV_CYCLE_S
        if rf and self.scenario.repeat:
            self.logs.put_nowait(
                "# disabling -R because replay would make GPS time jump backwards"
            )
            self.scenario.repeat = False

        if serial:
            self.scenario.serial = serial

        if self.generator is None:
            raise RuntimeError(
                "gps-sdr-sim was not found. Build it and set GPS_SDR_SIM to "
                "the full path of gps-sdr-sim.exe. Nothing is transmitted "
                "without it."
            )

        if rf and not shielded:
            raise RuntimeError(
                "refusing to transmit: confirm the rig is inside a shielded "
                "enclosure first"
            )

        files = self.rinex_files()
        eph = ephemeris or (files[-1] if files else None)
        if not eph:
            raise RuntimeError(
                "no RINEX navigation file in ./work. Copy a brdc*.22n "
                "(CDDIS broadcast ephemeris) or upload one from the UI."
            )

        if eph not in files:
            raise ValueError("choose a navigation file from the ephemeris list")
        try:
            nav_info = prepare_snapshot(self.workdir / eph, self.nav_path)
        except ValueError as exc:
            recommended = self.ephemeris_status()["recommended"]
            if not recommended:
                raise ValueError(f"{eph}: {exc}. Fetch a fresh navigation file.") from exc
            self.logs.put_nowait(
                f"# {eph} has invalid navigation headers ({exc}); using verified {recommended}"
            )
            eph = recommended
            nav_info = prepare_snapshot(self.workdir / eph, self.nav_path)
        available = shutil.disk_usage(self.workdir).free
        if self.iq_path.exists():
            available += self.iq_path.stat().st_size
        if self.scenario.est_bytes + 64*1024*1024 > available:
            raise ValueError(
                f"not enough disk space: waveform needs {self.scenario.est_bytes/1e9:.2f} GB"
            )

        when = self.scenario_time(eph)
        if not when:
            raise RuntimeError(
                f"could not read an epoch from {eph}. A RINEX nav file is "
                "required so the scenario time matches the ephemeris."
            )

        self.telemetry = {}
        self.telemetry["scenario_time"] = when
        self.telemetry["ephemeris_snapshot"] = nav_info
        self.telemetry["ephemeris_source"] = eph
        if self.scenario.scenario_time is None:
            gps_start = datetime.strptime(when, "%Y/%m/%d,%H:%M:%S").replace(tzinfo=timezone.utc)
            self.telemetry["rf_start_unix"] = gps_start.timestamp() - nav_info["leap_seconds"]
            self.telemetry["utc_start"] = datetime.fromtimestamp(
                self.telemetry["rf_start_unix"], timezone.utc
            ).isoformat()
        self.telemetry["time_mode"] = (
            "current GPS time (-T), aligned to UTC using navigation leap seconds"
            if self.scenario.scenario_time is None else "fixed (-t)"
        )
        self.started_at = time.time()
        self.state = "generating"
        self.progress = {"stage": "generating", "seconds": 0.0}
        self._task = asyncio.create_task(self._pipeline(
            rf, eph, when,
            overwrite_time=self.scenario.scenario_time is None,
        ))
        return self.snapshot()

    async def _pipeline(self, rf: bool, eph: str, when: str,
                        *, overwrite_time: bool) -> None:
        try:
            self.state = "generating"
            self.progress = {"stage": "generating", "seconds": 0.0}
            gen = build_generate_cmd(
                self.generator, self.scenario, self.nav_path,
                self.iq_path, when, overwrite_time=overwrite_time,
            )
            self.logs.put_nowait(f"$ {' '.join(gen)}")
            rc = await self._run(gen, stage="generate")

            if rc != 0:
                self.logs.put_nowait(f"!! generation failed (exit {rc})")
                return

            if len(self.telemetry.get("prns", [])) != SATELLITE_COUNT:
                self.logs.put_nowait("!! generator did not verify exactly six distinct PRNs; transmission blocked")
                return

            size = self.iq_path.stat().st_size if self.iq_path.is_file() else 0
            # gps-sdr-sim renders in 0.1-second blocks and may omit its final
            # block, so allow normal block rounding while rejecting empty,
            # truncated, or sample-misaligned output.
            if size < self.scenario.est_bytes * 0.8 or size % 2:
                self.logs.put_nowait(
                    f"!! invalid I/Q output: expected about {self.scenario.est_bytes} bytes, got {size}"
                )
                return
            self.logs.put_nowait(
                f"+ rendered {size/1e6:.1f} MB of L1 I/Q "
                f"({self.scenario.duration_s}s @ "
                f"{self.scenario.sample_rate/1e6:.2f} MS/s)"
            )
            if not rf:
                self.logs.put_nowait("+ dry run complete - no radio touched")
                return

            if overwrite_time:
                target = self.telemetry["rf_start_unix"]
                delay = target - time.time()
                if delay > 0:
                    self.progress = {"stage": "synchronizing", "seconds": delay}
                    self.logs.put_nowait(
                        f"# waiting {delay:.1f}s for RF start {self.telemetry['utc_start']}"
                    )
                    await asyncio.sleep(delay)
                else:
                    self.logs.put_nowait("!! render missed its scheduled start; rerun with a lower sample rate")
                    return

            if self.transfer is None:
                self.logs.put_nowait(
                    "!! hackrf_transfer not found - cannot transmit. Set "
                    "HACKRF_TRANSFER to its full path."
                )
                return

            self.state = "transmitting"
            self.progress = {"stage": "transmitting", "seconds": 0.0}
            tx = build_transfer_cmd(self.transfer, self.scenario, self.iq_path)
            self.logs.put_nowait(f"$ {' '.join(tx)}")
            self.logs.put_nowait(
                f"*** Starting sample transfer on {self.scenario.freq_hz/1e6:.3f} MHz; "
                "receiver acquisition remains unverified ***"
            )
            rc = await self._run(tx, stage="transfer", stream_output=True)
            self.telemetry["transfer_exit_code"] = rc
            if rc != 0:
                self.logs.put_nowait(f"!! transfer failed (exit {rc})")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logs.put_nowait(f"!! {exc}")
        finally:
            self.state = "idle"
            self.started_at = 0.0
            self.progress = {"stage": "idle", "seconds": 0.0}
            if self.on_idle is not None:
                await self.on_idle()

    async def _run(self, cmd: list[str], stage: str,
                   stream_output: bool = False) -> int:
        windows_options = {}
        if os.name == "nt":
            if stage == "transfer":
                # CREATE_NEW_PROCESS_GROUP ignores Ctrl+C. Give the transfer
                # its own hidden console so a targeted helper can deliver SIGINT
                # and allow hackrf_stop_tx/hackrf_close to release WinUSB.
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                windows_options = {"creationflags": subprocess.CREATE_NEW_CONSOLE,
                                   "startupinfo": startup}
            else:
                windows_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workdir),
            **windows_options,
        )
        self._proc = proc
        self._proc_stage = stage

        async def pump(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                if stage == "transfer":
                    match = re.search(r"([\d.]+) MiB /\s*([\d.]+) sec", line)
                    if match:
                        amount, duration = map(float, match.groups())
                        self.telemetry["transfer_mib_s"] = amount / max(duration, 0.001)
                        self.telemetry["last_transfer_report"] = time.time()
                    self.telemetry["transfer_last_line"] = line

                if stage == "generate":
                    if line.startswith("LAB_CHANNELS "):
                        prns = [int(p) for p in line.split()[1].split(",")]
                        self.telemetry["prns"] = sorted(set(prns))
                        self.telemetry["channels"] = []
                        self.logs.put_nowait(f"# six satellite PRNs: {prns}")
                        continue
                    # "Time into run = 1.5" is progress, not news.
                    m = _TIME_RE.search(line)
                    if m:
                        self.progress["seconds"] = float(m.group(1))
                        continue
                    # Per-satellite channel lines feed the UI table.
                    if _SATS_RE.match(line):
                        self.telemetry.setdefault("channels", []).append(line.strip())
                        continue
                    if line.startswith(("Using", "xyz", "llh", "Start time",
                                        "Duration")):
                        self.logs.put_nowait(f"# {line}")
                        continue

                self.logs.put_nowait(line)

        pumps = [
            asyncio.create_task(pump(proc.stdout)),
            asyncio.create_task(pump(proc.stderr)),
        ]
        try:
            rc = await proc.wait()
            await asyncio.gather(*pumps)
            return rc
        finally:
            for p in pumps:
                p.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*pumps, return_exceptions=True)

    async def stop(self, reason: str = "stopped by operator") -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            self.logs.put_nowait(f"# {reason} - stopping transmitter")
            try:
                if os.name == "nt":
                    if self._proc_stage == "transfer":
                        helper = await asyncio.create_subprocess_exec(
                            sys.executable, str(Path(__file__).parent / "tools" / "windows_ctrl_c.py"),
                            str(proc.pid), stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        try:
                            await asyncio.wait_for(helper.wait(), timeout=2)
                        except asyncio.TimeoutError:
                            helper.kill()
                            await helper.wait()
                    else:
                        proc.terminate()
                else:
                    proc.terminate()
            except (ProcessLookupError, ValueError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
                if os.name == "nt" and proc.returncode is None:
                    # A wedged libusb transfer can survive TerminateProcess.
                    # Ensure it cannot keep the HackRF handle orphaned.
                    with contextlib.suppress(Exception):
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=3,
                            check=False,
                        )

        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._task = None

        self._proc = None
        self.state = "idle"
        self.started_at = 0.0

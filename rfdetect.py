"""
HackRF presence detection.

Runs before any session starts so the operator knows whether there is
actually a radio on the bus. Checks, in order:

  1. `hackrf_info`      - libhackrf, authoritative if installed
  2. `SoapySDRUtil --find` - catches devices Soapy would use even when
                         libhackrf tools are absent (e.g. libhackrf built
                         into SoapySDK)

Both are optional; if neither exists we report "unknown" rather than
guessing, because a missing tool is not the same as an absent radio.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path

HACKRF_INFO = os.environ.get("HACKRF_INFO", "hackrf_info")
SOAPY_UTIL = os.environ.get("SOAPY_UTIL", "SoapySDRUtil")
GPS_SDR_SIM = "gps-sdr-sim"
HACKRF_TRANSFER = "hackrf_transfer"


def generator_installed() -> tuple[bool, str | None]:
    """Is the GPS-SDR-sim binary actually reachable?

    Checked separately from radio presence because they fail differently:
    a missing generator means nothing is ever transmitted regardless of
    what radio is plugged in, and the operator needs to be told which.
    """
    from toolpaths import resolve

    exe = resolve("gps_sdr_sim")
    return (exe is not None), exe


def transfer_installed() -> tuple[bool, str | None]:
    """hackrf_transfer, which this gps-sdr-sim fork uses to transmit."""
    from toolpaths import resolve

    exe = resolve("hackrf_transfer")
    return (exe is not None), exe

# Soapy driver name for the HackRF, and the USB IDs libhackrf uses.
HACKRF_DRIVERS = ("hackrf", "HackRF")
HACKRF_USB_IDS = (("0x1d50", "0x6089"), ("0x1d50", "0x608a"), ("0x1d50", "0x6090"))


@dataclass
class HackRfStatus:
    present: bool | None = None        # None = could not determine
    serial: str | None = None
    firmware: str | None = None
    driver: str | None = None
    source: str | None = None         # which probe produced this
    tool_missing: list[str] = field(default_factory=list)
    error: str | None = None
    devices: list[dict] = field(default_factory=list)
    driver_installed: bool = False    # SoapyHackRF module present
    generator_installed: bool = False  # gps-sdr-sim binary reachable
    generator_path: str | None = None
    transfer_installed: bool = False   # hackrf_transfer binary reachable
    transfer_path: str | None = None

    @property
    def label(self) -> str:
        if self.error:
            return self.error
        if self.present is True:
            return f"HackRF found{f' (serial {self.serial})' if self.serial else ''}"
        if self.present is False:
            return "no HackRF detected"
        return "HackRF status unknown"

    @property
    def blocking(self) -> str | None:
        """The one thing stopping transmission, or None if nothing is."""
        if self.error:
            return "radio"
        if self.present is not True:
            return "radio"
        if not self.generator_installed:
            return "generator"
        if not self.transfer_installed:
            return "transfer"
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        d["blocking"] = self.blocking
        return d


async def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a probe, returning (returncode, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError):
        return -1, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib_suppress():
            proc.kill()
        return -2, "probe timed out"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


_SERIAL_RE = re.compile(r"Serial number:\s*(\S+)", re.IGNORECASE)
_FW_RE = re.compile(r"Firmware version:\s*(\S+)", re.IGNORECASE)
_BOARD_RE = re.compile(r"Board ID Number:\s*(\d+)", re.IGNORECASE)

# libhackrf prints one of these when nothing is attached. The wording
# varies by version, so match the whole family rather than a fragment.
_NEGATIVE_RE = re.compile(
    r"no\s+hackrf\s+boards?\s+found|no\s+boards?\s+found|found\s+0\s+boards?|"
    r"board\s+count\s*:\s*0",
    re.IGNORECASE,
)
# A positive count, as printed by libhackrf. Note that 2024.x prints a
# bare "Found HackRF" with no number at all, so the uncounted form counts
# as one board.
_POSITIVE_COUNT_RE = re.compile(
    r"found\s+(\d+)\s+hackrf|found\s+(\d+)\s+boards?|"
    r"boards?\s+found\s*:\s*(\d+)|hackrf\s+boards?\s+found\s*:\s*(\d+)",
    re.IGNORECASE,
)
# "Found HackRF" / "HackRF found" - present, but uncounted.
_UNCOUNTED_RE = re.compile(r"\bfound\s+hackrf\b|\bhackrf\s+found\b", re.IGNORECASE)


def _parse_board_count(out: str) -> int | None:
    """Number of boards libhackrf claims, or None if it didn't say.

    Deliberately strict: the version banner always contains the words
    'hackrf_info' and 'libhackrf', so any loose substring test reports a
    phantom board. We only trust an explicit count.
    """
    low = out
    if _NEGATIVE_RE.search(low):
        return 0

    m = _POSITIVE_COUNT_RE.search(low)
    if m:
        for g in m.groups():
            if g is not None:
                return int(g)

    # libhackrf 2024.x prints "Found HackRF" with no number when one board
    # is attached. Only trust this when a detail block corroborates it, so
    # the phrase alone can't mask a later "no boards" line.
    if _UNCOUNTED_RE.search(out) and (_SERIAL_RE.search(out) or _BOARD_RE.search(out)):
        return len(_SERIAL_RE.findall(out)) or 1

    # No explicit count. Fall back to per-board detail blocks, which only
    # appear when a board was actually opened.
    if _BOARD_RE.search(out) or _SERIAL_RE.search(out):
        return len(_SERIAL_RE.findall(out)) or 1
    return None


async def detect() -> HackRfStatus:
    st = HackRfStatus()
    probed = False

    st.generator_installed, st.generator_path = generator_installed()
    st.transfer_installed, st.transfer_path = transfer_installed()

    # Probes resolve through toolpaths, so a radio is still detected when
    # radioconda/Library/bin is missing from PATH.
    from toolpaths import resolve

    info_exe = resolve("hackrf_info")
    soapy_exe = resolve("soapydr_util")

    # --- 1. libhackrf probe
    if info_exe:
        probed = True
        rc, out = await _run([info_exe])
        count = _parse_board_count(out)
        open_failed = bool(
            _UNCOUNTED_RE.search(out)
            and re.search(r"access denied|hackrf_open\(\) failed", out, re.IGNORECASE)
        )
        if open_failed:
            st.present = True
            st.source = "hackrf_info"
            st.error = (
                "HackRF is visible but cannot be opened. Another process owns it, "
                "or Windows has not released the USB handle; stop SDR apps and reconnect the board."
            )
        if count is not None and count > 0:
            st.present = True
            st.source = "hackrf_info"
            m = _SERIAL_RE.search(out)
            st.serial = m.group(1) if m else None
            m = _FW_RE.search(out)
            st.firmware = m.group(1) if m else None
            for m in _BOARD_RE.finditer(out):
                st.devices.append({"board_id": int(m.group(1))})
        if count == 0:
            # Authoritative negative from libhackrf. Still fall through to
            # Soapy, which may see a radio libhackrf cannot enumerate.
            st.present = False
            st.source = "hackrf_info"
        # count is None: libhackrf ran but said nothing conclusive; let
        # the Soapy probe decide rather than guessing either way.
    else:
        st.tool_missing.append(HACKRF_INFO)

    if st.present is True:
        # libhackrf saw a board, but we still want the Soapy driver
        # flag filled in for the UI, so don't return yet.
        pass

    # --- 2. SoapySDR probe
    if soapy_exe:
        probed = True

        # Is the SoapyHackRF driver even installed? Distinguishes
        # "no driver" from "driver present, nothing plugged in", which
        # need different fixes.
        rc_chk, chk_out = await _run([soapy_exe, "--check=hackrf"])
        st.driver_installed = "found" in chk_out.lower() or "yes" in chk_out.lower()

        rc, out = await _run([soapy_exe, "--find"])
        # Soapy mixes \n and \r\n; normalise before splitting so a block
        # that starts at offset 0 or follows a \r\n is still matched.
        text = out.replace("\r\n", "\n").replace("\r", "\n")
        for block in re.split(r"\n?Found device ", text)[1:]:
            fields = dict(re.findall(r"(\w+)\s*=\s*([^\n]+)", block))
            driver = fields.get("driver", "").strip()
            if driver in HACKRF_DRIVERS:
                st.present = True
                st.driver = driver
                st.source = st.source or "soapy"
                serial = fields.get("serial", "").strip() or None
                # Mirror the first unit's serial to the top level so the
                # Soapy device string can be pinned to it.
                st.serial = st.serial or serial
                st.devices.append(
                    {
                        "driver": driver,
                        "label": fields.get("label", "").strip(),
                        "serial": serial,
                    }
                )
        if st.present:
            return st
        # Soapy ran and saw no HackRF. If libhackrf already reported a
        # board, keep that positive result - one API seeing a device the
        # other misses is normal, not a contradiction.
        if st.present is None:
            st.present = False
            st.source = st.source or "soapy"
        return st

    # --- neither probe available
    if not probed:
        st.present = None
        st.error = (
            "neither hackrf_info nor SoapySDRUtil is on PATH - cannot verify "
            "whether a HackRF is connected"
        )
        return st

    st.present = False
    st.source = "combined"
    return st


def device_args(st: HackRfStatus) -> str | None:
    """SoapySDR device string, pinned to the serial when we know it.

    Pinning matters: with several radios attached, '-s hackrf' binds to
    whichever enumerates first, which is not necessarily the one you are
    holding next to the receiver.
    """
    if st.present and st.serial and st.driver:
        return f"{st.driver}:serial={st.serial}"
    return None

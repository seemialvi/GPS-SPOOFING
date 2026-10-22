"""
Toolchain resolution for gps-sdr-sim and hackrf_transfer.

Environment variables are a poor place to keep these: they have to be set
in every shell that starts the server, and a forgotten $env:GPS_SDR_SIM
shows up as a phantom "generator missing" even though the binary is
right there on disk.

Resolution order per tool:

  1. explicit environment variable
  2. config.json next to the app (editable from the UI)
  3. auto-discovery in known locations
  4. bare name on PATH

Auto-discovery searches a short list of plausible roots rather than
scanning the whole drive, so startup stays fast.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"

# name -> (env var, default bare name)
TOOLS = {
    "gps_sdr_sim": ("GPS_SDR_SIM", "gps-sdr-sim"),
    "hackrf_transfer": ("HACKRF_TRANSFER", "hackrf_transfer"),
    "hackrf_info": ("HACKRF_INFO", "hackrf_info"),
    "soapydr_util": ("SOAPY_UTIL", "SoapySDRUtil"),
}

# Where to look when nothing is configured. Checked in order; the first
# hit wins. Globs are relative to each root.
SEARCH_ROOTS = [
    APP_DIR,
    APP_DIR / "tools",
    APP_DIR / "bin",
    Path(r"E:\HackRF Learning Material\Git"),
    Path(r"E:\HackRF Learning Material"),
    Path(r"C:\Users\samee\radioconda\Library\bin"),
    Path(r"C:\Users\samee\radioconda\bin"),
    Path(r"C:\Program Files\HackRF"),
    Path(r"C:\Program Files (x86)\HackRF"),
    Path(r"C:\Program Files\SoapySDR"),
    Path(r"C:\hackrf"),
]

# Windows executables to look for per tool, and glob patterns.
GLOBS = {
    "gps_sdr_sim": ["gps-sdr-sim.exe", "gps-sdr-sim", "**/gps-sdr-sim.exe"],
    "hackrf_transfer": ["hackrf_transfer.exe", "hackrf_transfer"],
    "hackrf_info": ["hackrf_info.exe", "hackrf_info"],
    "soapydr_util": ["SoapySDRUtil.exe", "SoapySDRUtil"],
}


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def _discover(name: str) -> str | None:
    patterns = GLOBS.get(name, [])
    for root in SEARCH_ROOTS:
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for pattern in patterns:
            try:
                hit = next(root.glob(pattern), None)
            except (OSError, ValueError):
                continue
            if hit and hit.is_file():
                return str(hit)
    return None


def resolve(name: str) -> str | None:
    """Absolute path for a tool, or None if it cannot be found."""
    env_var, default = TOOLS.get(name, (name.upper(), name))

    # 1. environment
    target = os.environ.get(env_var)
    if target:
        exe = shutil.which(target)
        if exe:
            return exe
        candidate = Path(target).expanduser()
        if candidate.is_file():
            return str(candidate)

    # 2. config.json
    cfg = load_config()
    saved = cfg.get(name)
    if saved:
        candidate = Path(saved).expanduser()
        if candidate.is_file():
            return str(candidate)

    # 3. auto-discovery
    found = _discover(name)
    if found:
        return found

    # 4. bare name on PATH
    return shutil.which(default)


def status() -> dict:
    """Resolution state for every tool, for the UI and the preflight."""
    out = {}
    for name in TOOLS:
        path = resolve(name)
        env_var = TOOLS[name][0]
        out[name] = {
            "path": path,
            "found": path is not None,
            "source": (
                "env" if os.environ.get(env_var)
                else "config" if load_config().get(name) and Path(load_config()[name]).is_file()
                else "discovered" if path and not shutil.which(TOOLS[name][1]) else "path"
            ),
            "env_var": env_var,
        }
    return out


def set_tool(name: str, path: str | None) -> dict:
    """Persist a tool path (or clear it when path is None/empty)."""
    cfg = load_config()
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            # Allow a bare command name too.
            resolved = shutil.which(path)
            if not resolved:
                raise ValueError(f"no such file: {path}")
        cfg[name] = path
    else:
        cfg.pop(name, None)
    save_config(cfg)
    return status()
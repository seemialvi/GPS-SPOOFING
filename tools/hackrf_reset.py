"""Reset one explicitly selected HackRF through libhackrf (no RF, no flashing).

Invoked in a separate process so old firmware/USB timeouts cannot hang the UI.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import re


def reset_device(library: Path, serial: str) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", serial):
        raise ValueError("a full 32-digit HackRF serial is required")
    directory = os.add_dll_directory(str(library.parent)) if os.name == "nt" else None
    try:
        lib = ctypes.CDLL(str(library))
        device = ctypes.c_void_p()
        for name, args in {
            "hackrf_init": [], "hackrf_exit": [],
            "hackrf_open_by_serial": [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)],
            "hackrf_reset": [ctypes.c_void_p], "hackrf_close": [ctypes.c_void_p],
        }.items():
            func = getattr(lib, name)
            func.argtypes, func.restype = args, ctypes.c_int
        lib.hackrf_error_name.argtypes = [ctypes.c_int]
        lib.hackrf_error_name.restype = ctypes.c_char_p

        def checked(code, operation):
            if code != 0:
                label = lib.hackrf_error_name(code).decode(errors="replace")
                raise RuntimeError(f"{operation}: {label} ({code})")

        checked(lib.hackrf_init(), "hackrf_init")
        try:
            checked(lib.hackrf_open_by_serial(serial.encode(), ctypes.byref(device)), "open")
            try:
                checked(lib.hackrf_reset(device), "hackrf_reset")
            finally:
                lib.hackrf_close(device)
        finally:
            lib.hackrf_exit()
        return {"ok": True, "serial": serial, "method": "libhackrf firmware reset"}
    finally:
        if directory is not None:
            directory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(reset_device(args.dll.resolve(), args.serial)))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)

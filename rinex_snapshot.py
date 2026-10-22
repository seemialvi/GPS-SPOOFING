"""Prepare a coherent RINEX 2 epoch set for gps-sdr-sim's -T mode.

The upstream parser groups records by TOC. An isolated old record at the
start of a daily file otherwise becomes the time-rewrite anchor and can
leave the selected group with zero visible satellites.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import math

NUMBER = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[dDeE][-+]?\d+)?")


def prepare_snapshot(source: Path, destination: Path | None = None) -> dict:
    lines = source.read_text(encoding="ascii", errors="strict").splitlines()
    end = next((i for i, line in enumerate(lines) if "END OF HEADER" in line), None)
    if end is None:
        raise ValueError("RINEX navigation header is missing END OF HEADER")
    if not lines or not lines[0].lstrip().startswith("2"):
        raise ValueError("six-satellite mode requires RINEX 2 GPS navigation data")
    header = []
    leap_seconds = None
    seen = set()
    for line in lines[:end+1]:
        label = next((label for label in (
            "ION ALPHA", "ION BETA", "DELTA-UTC: A0,A1,T,W", "LEAP SECONDS", "END OF HEADER"
        ) if label in line), None)
        if label:
            seen.add(label)
            value = line[:line.index(label)]
            fields = [float(v.replace("D", "E").replace("d", "e"))
                      for v in NUMBER.findall(value)]
            if not all(math.isfinite(v) for v in fields):
                raise ValueError(f"non-finite {label} header")
            if label == "DELTA-UTC: A0,A1,T,W":
                if len(fields) != 4:
                    raise ValueError("invalid DELTA-UTC header")
                if not 0 <= fields[2] < 604800 or fields[2] % 4096:
                    raise ValueError("UTC reference time must be a multiple of 4096 seconds within the GPS week")
                if fields[3] <= 0 or fields[3] != int(fields[3]):
                    raise ValueError("UTC reference week is missing or invalid")
                if not -(2**23) <= fields[1] / (2**-50) < 2**23:
                    raise ValueError("UTC drift cannot be represented in LNAV")
                value = (f"   {fields[0]:19.12E}{fields[1]:19.12E}"
                         f"{int(fields[2]):9d}{int(fields[3]):9d}").replace("E", "D")
            elif label in ("ION ALPHA", "ION BETA"):
                if len(fields) != 4:
                    raise ValueError(f"invalid {label} header")
                value = "  " + "".join(f"{v:12.4E}" for v in fields).replace("E", "D")
            elif label == "LEAP SECONDS":
                leap_seconds = int(fields[0])
                value = f"{leap_seconds:6d}"
            header.append(f"{value:<60}{label}")
        else:
            header.append(line)
    if leap_seconds is None or not 0 <= leap_seconds <= 64:
        raise ValueError("a valid LEAP SECONDS header is required for GPS/UTC alignment")
    missing = {"ION ALPHA", "ION BETA", "DELTA-UTC: A0,A1,T,W"} - seen
    if missing:
        raise ValueError("missing navigation headers: " + ", ".join(sorted(missing)))
    latest = {}
    data = lines[end+1:]
    if len(data) % 8:
        raise ValueError("truncated RINEX navigation record")
    for i in range(0, len(data), 8):
        block = data[i:i+8]
        try:
            row = block[0]
            prn = int(row[:2])
            year = int(row[3:5])
            epoch = datetime(2000+year if year<80 else 1900+year,
                             int(row[6:8]), int(row[9:11]), int(row[12:14]),
                             int(row[15:17]), int(float(row[18:22])))
            if not 1 <= prn <= 32:
                raise ValueError("PRN outside GPS 1..32")
            if prn not in latest or epoch > latest[prn][0]:
                latest[prn] = (epoch, block)
        except (ValueError, IndexError) as exc:
            raise ValueError(f"invalid RINEX record at line {end+2+i}: {exc}") from exc
    if not latest:
        raise ValueError("no GPS ephemeris records")
    counts = Counter(epoch for epoch, _ in latest.values())
    anchor = max(counts, key=lambda epoch: (counts[epoch], epoch))
    # Keep one coherent set. -T now shifts this entire set by the same amount.
    chosen = [(epoch, prn, block) for prn, (epoch, block) in latest.items()
              if abs((epoch-anchor).total_seconds()) < 3600]
    if len(chosen) < 6:
        raise ValueError(f"only {len(chosen)} satellites have coherent ephemeris; need at least six")
    text = "\n".join(header + [line for _, _, block in sorted(chosen) for line in block]) + "\n"
    if destination is not None:
        destination.write_text(text, encoding="ascii", newline="\n")
    return {"epoch": anchor.strftime("%Y/%m/%d,%H:%M:%S"),
            "records": len(chosen), "leap_seconds": leap_seconds}

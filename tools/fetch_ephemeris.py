"""
Fetch today's broadcast ephemeris and convert it for gps-sdr-sim.

    python tools/fetch_ephemeris.py [days_back]

Downloads the daily RINEX navigation file from the BKG mirror (no login
required, unlike CDDIS), converts it to RINEX 2 with
rinex3_to_rinex2.py, and drops it in ./work.

Run this daily: a receiver ignores a signal whose ephemerides do not
correspond to its own clock, so yesterday's data silently does nothing.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from rinex3_to_rinex2 import convert  # noqa: E402

BKG = "https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy}/"
NAME = "BRDC00WRD_R_{year}{doy:03d}0000_01D_MN.rnx.gz"
WORK = Path(__file__).resolve().parent.parent / "work"


def main(days_back: int = 0) -> int:
    day = dt.date.today() - dt.timedelta(days=days_back)
    year, doy = day.year, day.timetuple().tm_yday
    url = BKG.format(year=year, doy=doy) + NAME.format(year=year, doy=doy)

    print(f"day   : {day} (DOY {doy})")
    print(f"fetch : {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            raw = r.read()
    except Exception as exc:
        print(f"ERROR : {exc}")
        print("Try another day:  python tools/fetch_ephemeris.py 1")
        return 1

    print(f"got   : {len(raw):,} bytes")
    WORK.mkdir(parents=True, exist_ok=True)

    src = WORK / f"_src_{day.year}{doy:03d}.rnx.gz"
    dst = WORK / f"brdc{doy:03d}0.{day.year % 100:02d}n"
    src.write_bytes(raw)

    total, kept = convert(src, dst)
    src.unlink(missing_ok=True)
    print(f"wrote : {dst.name} ({kept} GPS satellites from {total} records)")

    from spoof_engine import rinex_epoch

    print(f"epoch : {rinex_epoch(dst)}   (use this for -t)")
    return 0


if __name__ == "__main__":
    back = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    sys.exit(main(back))
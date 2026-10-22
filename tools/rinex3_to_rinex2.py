"""
Convert a RINEX 3 navigation file to RINEX 2.

gps-sdr-sim's parser is RINEX 2 only: fixed columns, labels at column 61,
eight lines per satellite. Modern broadcast files (BKG, CDDIS, IGN) are
RINEX 3.05 with one line per record and mixed constellations, which it
cannot read at all.

Both formats list the ephemeris fields in the same order, so this is a
re-layout plus a filter down to GPS - gps-sdr-sim synthesises L1 C/A only.

    python tools/rinex3_to_rinex2.py <in.rnx[.gz]> <out.22n>

Only the most recent record per satellite is kept. gps-sdr-sim stores
ephemeris as eph[week][prn], so a later record for the same satellite
overwrites an earlier one and only the newest survives into the signal.
Emitting just that one per PRN puts every satellite's clock reference in
the same few minutes, so one -t value lands them all inside the +/-1 hour
acceptance window.
"""

from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

# A RINEX 3 nav record: constellation char, PRN, then the epoch. The
# match must stop immediately after the seconds field - anything that
# also consumes the following token would swallow af0, since the values
# start directly after the epoch with no separating space.
REC_RE = re.compile(
    r"^([A-Z])(\d{2})\s+"
    r"(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})"
)

# RINEX 3 uses fixed-width 19-character fields with no separator, so a
# negative value butts straight against its predecessor
# ("1.18e+02-6.02e+01"). Splitting on whitespace merges them into one
# bogus number, so match the number grammar itself.
NUMBER_RE = re.compile(r"[-+]?\d+\.\d*[eEdD][-+]?\d+")


def fmt(value: str) -> str:
    """Render one value into exactly 19 characters.

    RINEX 2 fields are a fixed 19 columns wide and the simulator reads
    them by hard-coded offset (af0 at 22, af1 at 41, af2 at 60). Any field
    that comes out 18 or 20 characters shifts every later field, and the
    record is silently dropped as an invalid ephemeris. So the width is
    not a cosmetic choice here.
    """
    try:
        num = float(value.replace("D", "E").replace("d", "e"))
    except ValueError:
        return f"{value:>19.19}"
    if num == 0:
        return " 0.000000000000D+00"
    # {num:19.12E} is exactly 19 columns.
    return f"{num:19.12E}".replace("E", "D")


HEADER = [
    ("     2              NAVIGATION DATA", "RINEX VERSION / TYPE"),
    ("converted from RINEX 3 by rinex3_to_rinex2.py", "PGM / RUN BY / DATE"),
]


def convert(src: Path, dst: Path, constellation: str = "G") -> tuple[int, int]:
    opener = gzip.open if src.suffix == ".gz" else open
    total = 0

    # prn -> (epoch key, rendered record text)
    latest: dict[int, tuple[tuple, str]] = {}

    with opener(src, "rt", errors="replace") as fh:
        # Preserve actual GPS corrections; fabricated constants are not valid
        # substitutes for the source's UTC/ionosphere parameters.
        gps_header = {}
        for line in fh:
            if "END OF HEADER" in line:
                break
            label = line[60:].strip()
            values = line[:60].split()
            if label == "IONOSPHERIC CORR" and values[0] in ("GPSA", "GPSB"):
                nums = [float(v.replace("D", "E")) for v in values[1:5]]
                name = "ION ALPHA" if values[0] == "GPSA" else "ION BETA"
                gps_header[name] = "  " + "".join(f"{v:12.4E}" for v in nums).replace("E", "D")
            elif label == "TIME SYSTEM CORR" and values[0] == "GPUT":
                # RINEX 3 uses 17/16-column fields without separators.
                a0 = float(line[5:22].replace("D", "E"))
                a1 = float(line[22:38].replace("D", "E"))
                tot, week = int(line[38:45]), int(line[45:50])
                gps_header["DELTA-UTC: A0,A1,T,W"] = (
                    f"   {a0:19.12E}{a1:19.12E}{tot:9d}{week:9d}"
                ).replace("E", "D")
            elif label == "LEAP SECONDS":
                gps_header[label] = f"{int(values[0]):6d}"

        block: list[str] = []
        for line in fh:
            if not line.strip():
                continue
            m = REC_RE.match(line)

            if m:
                total += 1
                block = []                      # a new record voids any partial
                if m.group(1) != constellation:
                    continue
                prn = int(m.group(2))
                y, mo, d, hh, mm, ss = (int(m.group(i)) for i in range(3, 9))
                # gps-sdr-sim reads this line at a 3-character pitch:
                # PRN [0:2], YY [3:5], MM [6:8], DD [9:11], hh [12:14],
                # mm [15:17], ss [18:22] - then af0 immediately at [22].
                # Any other spacing shifts every field and the record is
                # silently discarded as invalid.
                head = (f"{prn:>2} {y % 100:02d} {mo:>2} {d:>2}"
                        f" {hh:>2} {mm:>2} {ss:>4.1f}")
                block = [head, line[m.end():].strip()]
            elif block:
                block.append(line.strip())

            if block and len(block) >= 9:
                values = NUMBER_RE.findall(" ".join(block[1:]))
                # RINEX 3 often blanks the tail of the final line (fit
                # interval and the two spare fields), so a complete record
                # can carry as few as 28 numbers. Pad to 31 rather than
                # dropping the record.
                if len(values) >= 28:
                    values = values[:31] + ["0"] * (31 - len(values))
                    # values[0:3] are af0/af1/af2 and share the epoch
                    # line; the remaining 28 fill seven 4-per-line rows.
                    rec = (f"{block[0]}{fmt(values[0])}"
                           f"{fmt(values[1])}{fmt(values[2])}\n")
                    for row in range(1, 8):
                        v = values[3 + (row - 1) * 4: 3 + row * 4]
                        rec += (f"   {fmt(v[0])}{fmt(v[1])}"
                                f"{fmt(v[2])}{fmt(v[3])}\n")
                    epoch = (y, mo, d, hh, mm, ss)
                    if prn not in latest or epoch > latest[prn][0]:
                        latest[prn] = (epoch, rec)
                block = []

    with dst.open("w", newline="\n") as out:
        for value, label in HEADER:
            out.write(f"{value:<60}{label}\n")
        for label, value in gps_header.items():
            out.write(f"{value:<60}{label}\n")
        out.write(f"{'':60}END OF HEADER\n")
        for _, (_, rec) in sorted(latest.items(), key=lambda kv: kv[1][0]):
            out.write(rec)

    return total, len(latest)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    total, kept = convert(src, dst)
    print(f"{src.name}: {total} records, kept {kept} GPS (latest per PRN)"
          f" -> {dst.name}")

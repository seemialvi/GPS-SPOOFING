"""
Stub that impersonates gps-sdr-sim: validates args, prints telemetry lines
for a few seconds, then exits on its own. Exercises the full start/stop
lifecycle without any radio attached.
"""

import argparse
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("-c", "--config")
p.add_argument("-e", "--ephemeris")
p.add_argument("-b", "--binary")
p.add_argument("-s", "--soapy")
p.add_argument("-t", "--time")
args = p.parse_args()

missing = [n for n, v in (("-c", args.config), ("-e", args.ephemeris)) if not v]
if missing:
    print(f"ERROR: missing required argument(s) {missing}", file=sys.stderr)
    sys.exit(1)

try:
    cfg = open(args.config).read()
    open(args.ephemeris).read()
except OSError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

lat = lon = None
for line in cfg.splitlines():
    k, _, v = line.partition(",")
    if k == "START_LAT":
        lat = v
    elif k == "START_LON":
        lon = v

print(f"Using ephemeris {args.ephemeris}")
print(f"Using config {args.config}")
if args.soapy:
    print(f"Using device {args.soapy}")
if args.binary:
    with open(args.binary, "wb") as fh:
        fh.write(b"\x00" * 8192)
    print(f"Writing binary samples to {args.binary}")
print("Generating GPS L1 signal...")

for _ in range(6):
    print(f"Latitude, Longitude (degrees)  : {lat}, {lon}")
    print("Altitude (m)                   : 58.0")
    time.sleep(1)

print("Statistics: 6 samples generated")
sys.exit(0)
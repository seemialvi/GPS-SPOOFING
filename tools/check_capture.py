"""Offline C/A acquisition check of signed 8-bit I/Q; never opens an SDR.

Usage: python tools/check_capture.py work/gpssim.bin --prns 1,3,6,8,26,28
Requires numpy. Reports correlation peak / mean acquisition power.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# IS-GPS-200 Table 3-Ia: G2 output tap pairs, PRNs 1..32.
TAPS = [(2,6),(3,7),(4,8),(5,9),(1,9),(2,10),(1,8),(2,9),
        (3,10),(2,3),(3,4),(5,6),(6,7),(7,8),(8,9),(9,10),
        (1,4),(2,5),(3,6),(4,7),(5,8),(6,9),(1,3),(4,6),
        (5,7),(6,8),(7,9),(8,10),(1,6),(2,7),(3,8),(4,9)]


def ca_code(prn):
    a, b = TAPS[prn-1]
    g1, g2 = [1]*10, [1]*10
    chips = []
    for _ in range(1023):
        chips.append(1-2*(g1[9]^g2[a-1]^g2[b-1]))
        x = g1[2]^g1[9]
        y = g2[1]^g2[2]^g2[5]^g2[7]^g2[8]^g2[9]
        g1, g2 = [x]+g1[:9], [y]+g2[:9]
    return np.array(chips)


def acquire(path, rate=2600000):
    size = rate//1000
    with Path(path).open("rb") as stream:
        stream.seek(int(rate*.020)*2)
        raw = np.frombuffer(stream.read(size*2*10), dtype=np.int8).astype(float)
    iq = (raw[0::2]+1j*raw[1::2]).reshape(10,size)
    seconds = np.arange(size)/rate
    indexes = np.floor(np.arange(size)*1023000/rate).astype(int)
    spectra = {}
    for doppler in range(-6000,6001,500):
        spectra[doppler] = np.fft.fft(iq*np.exp(-2j*np.pi*doppler*seconds), axis=1)
    results = []
    for prn in range(1,33):
        code = np.conj(np.fft.fft(ca_code(prn)[indexes]))
        best = None
        for doppler, spectrum in spectra.items():
            correlation = np.fft.ifft(spectrum*code, axis=1)
            power = np.sum(np.abs(correlation)**2, axis=0)
            peak = int(np.argmax(power))
            ratio = float(power[peak]/np.mean(power))
            if best is None or ratio>best["peak_to_mean"]:
                best = {"prn":prn,"doppler_hz":doppler,"sample_phase":peak,"peak_to_mean":ratio}
        results.append(best)
    return sorted(results,key=lambda r:r["peak_to_mean"],reverse=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture",type=Path)
    parser.add_argument("--prns",required=True)
    args = parser.parse_args()
    expected = {int(p) for p in args.prns.split(',')}
    result = acquire(args.capture)
    detected = {r["prn"] for r in result[:6]}
    print(json.dumps({"expected":sorted(expected),"strongest_six":result[:6],
                      "next_strongest":result[6],"pass":detected==expected},indent=2))
    raise SystemExit(0 if detected==expected and min(r["peak_to_mean"] for r in result[:6])>8 else 1)

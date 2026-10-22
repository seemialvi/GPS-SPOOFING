# GNSS Spoof Lab

A Windows-oriented GNSS bench simulator with a pastel pixel-art web interface, a modified six-channel GPS L1 C/A generator, HackRF controls, and a live NEO-6M receiver monitor.

Use RF features only with a conducted, attenuated connection or a properly shielded laboratory setup. Keep the web server bound to localhost; it has no authentication.

## Project status

The repository includes signal generation, navigation-message checks, radio lifecycle controls, and receiver telemetry. **Successful receiver acquisition at the requested coordinates has not been demonstrated.** A receiver losing its fix during transmission is not evidence that it acquired the simulated signal. Software tests validate specific implementation behavior, not end-to-end RF performance or complete GPS compliance.

## Features

- Light, kawaii pixel theme with a retro gaming layout.
- Coordinates and scenario controls, render progress, hardware checks, and streamed logs.
- Local gps-sdr-sim fork requiring six healthy visible satellite channels.
- Satellite-specific C/A codes, navigation data, pseudorange, and Doppler.
- LNAV paging across a 25-frame cycle, with ionosphere/UTC header validation.
- Graceful Windows transfer stopping and a five-minute HackRF reset scheduler. Resets wait for idle by default; interrupt mode stops the session first.
- Read-only serial monitoring for raw NMEA or ESP32/TinyGPS++ text output, with stale-fix handling and recent raw lines.

## Requirements

- Windows with Python 3.11 or newer. Development and hardware paths were exercised on Windows; other platforms are not verified.
- Visual Studio C++ Build Tools to compile the bundled native generator and navigation test probe.
- HackRF command-line tools and matching libhackrf DLL for radio controls. Hardware is not needed for parser and offline generation tests.
- A serial connection to the receiver or its ESP32 bridge for live receiver monitoring.
- Approximately 4 GB of free space per default 750-second capture, plus working space.

## Setup

```powershell
git clone https://github.com/seemialvi/GPS-SPOOFING.git
cd GPS-SPOOFING
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Compile the bundled generator:

```powershell
.\tools\build_generator.cmd
```

The build scripts currently expect `vcvars64.bat` at `C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat`. Adjust that path in both build scripts if your Visual Studio installation differs. Build this repository's modified source; an unmodified upstream executable does not provide the same six-channel behavior.

Start the local application:

```powershell
.\.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080>. The UI provides tool-path settings. Tools can also be configured through `GPS_SDR_SIM`, `HACKRF_TRANSFER`, `HACKRF_INFO`, and `SOAPY_UTIL` environment variables. Resolution checks environment variables, local `config.json`, known installation locations, and PATH. The bundled generator builds into `bin/gps-sdr-sim.exe`.

`config.example.json` contains the maintenance defaults. Copy it to `config.json` if needed; the app saves local preferences there. `LAB_WORKDIR` optionally changes the working-data directory.

## Navigation data and rendering

Upload suitable broadcast navigation data through the UI, or use the included downloader:

```powershell
.\.venv\Scripts\python.exe tools/fetch_ephemeris.py
```

The downloader retrieves a daily RINEX file from the BKG mirror and converts GPS records to RINEX 2 under `work/`. Availability depends on the upstream service; the script accepts a number of days back as an argument. The committed navigation fixture is historical test data, not a current operational navigation file.

The engine validates the navigation headers, prepares a coherent snapshot, and invokes the bundled native generator. Rendering creates a headerless file of interleaved signed 8-bit I/Q samples at 2.6 million complex samples per second. This consumes 5.2 MB per second: the default 750-second capture is 3,900,000,000 bytes (about 3.63 GiB).

Render and transmit are separate stages. After rendering and capture checks, an RF session uses `hackrf_transfer` to replay the file. The engine schedules a GPS-time start and blocks a missed start. A host transfer rate or successful subprocess exit does not establish that a receiver decoded the waveform.

The navigation profile uses 50 bits per second: 30-bit words, 300-bit / 6-second subframes, five subframes per 30-second frame, and a 25-frame / 750-second paging cycle. See [the native change notes](vendor/gps-sdr-sim/LAB_CHANGES.md) for limitations. The engine does not synchronize to a receiver's existing tracking loops or implement a demonstrated tracking takeover.

## Receiver monitor

Connect the receiver's serial bridge to the PC, close other serial-monitor applications, then select a port and baud rate in the receiver panel. ESP32 bridges commonly use 115200 baud; direct receiver UART settings depend on its configuration.

Supported inputs include checksummed NMEA GGA, RMC, GSA, and GSV, or text such as:

```text
Latitude: 31.451852
Longitude: 74.291959
Satellites: 4
HDOP: 2.66
```

The monitor recognizes `No fresh GPS position` as an invalid fix. It displays current position, satellites, HDOP, and available NMEA details. Invalid, disconnected, or stale input clears the current coordinates and retains the last known fix separately. For custom text output, the firmware must identify stale GPS data; receiving a fresh serial line alone cannot prove a fresh GPS fix.

Monitoring is read-only and independent of transmission. It does not send receiver configuration commands. Live serial access requires exclusive access to the selected port.

## Tests

Build both native executables before running the test suite:

```powershell
.\tools\build_generator.cmd
.\tools\build_nav_probe.cmd
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Coverage includes navigation parity/paging, short offline waveform generation, navigation-file validation, receiver parsing/freshness, maintenance scheduling, and graceful Windows subprocess stopping. These tests do not transmit RF.

An optional browser check requires the server on port 8080 and Chrome at the Windows installation path used in the script:

```powershell
.\.venv\Scripts\python.exe tests/browser_receiver_check.py
```

It saves desktop/mobile screenshots under `build/`. The optional offline capture inspection utility, `tools/check_capture.py`, additionally requires NumPy.

## Source layout

- `server.py`: FastAPI routes and WebSocket updates.
- `static/index.html`: application interface.
- `spoof_engine.py`, `rinex_snapshot.py`: rendering/session management and navigation validation.
- `receiver_monitor.py`: serial input and receiver state.
- `radio_maintenance.py`, `rfdetect.py`, `toolpaths.py`: maintenance, detection, and tool configuration.
- `vendor/gps-sdr-sim/`: modified native generator, upstream license, and change notes.
- `tools/`: build, conversion, inspection, and Windows hardware helpers.
- `tests/`: automated checks and a historical navigation fixture.

Generated captures, downloaded navigation files, binaries, build outputs, virtual environments, caches, and local configuration are excluded from Git. They are recreated locally rather than uploaded to GitHub.

## Upstream attribution

The native generator derives from [osqzss/gps-sdr-sim](https://github.com/osqzss/gps-sdr-sim). Its MIT license is preserved in [vendor/gps-sdr-sim/LICENSE](vendor/gps-sdr-sim/LICENSE); retain the notices in the bundled sources. No additional license is assigned to the application code by this README.

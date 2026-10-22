"""
Web layer: REST for control, WebSocket for streaming (logs, telemetry, NMEA).

    uvicorn server:app --host 127.0.0.1 --port 8080

Binds to localhost on purpose - this must not be exposed on a network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import rfdetect
import toolpaths
from radio_maintenance import RadioMaintenance
from receiver_monitor import ReceiverMonitor, available_ports
from spoof_engine import Scenario, SpoofEngine

APP_DIR = Path(__file__).parent
WORK_DIR = Path(os.environ.get("LAB_WORKDIR", APP_DIR / "work"))

engine = SpoofEngine(WORK_DIR)
receiver = ReceiverMonitor()
engine.on_idle = lambda: _push_state()  # push final state when the generator exits

app = FastAPI(title="GNSS spoof lab", version="0.3")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

# Cached preflight result, refreshed on demand.
_hardware: rfdetect.HackRfStatus | None = None
_hardware_lock = asyncio.Lock()


async def _reset_hackrf() -> dict:
    st = await hardware_status(force=True)
    if st.present is not True or not st.serial or st.error:
        raise RuntimeError(st.error or "no accessible HackRF was found")
    info = toolpaths.resolve("hackrf_info")
    if not info:
        raise RuntimeError("hackrf_info is required to locate the matching libhackrf")
    library = Path(info).parent / "hackrf-0.dll"
    if not library.is_file():
        raise RuntimeError(f"libhackrf was not found at {library}")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(APP_DIR / "tools" / "hackrf_reset.py"),
        "--dll", str(library), "--serial", st.serial,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("HackRF reset timed out; reconnect the USB cable") from exc
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    output = raw.decode(errors="replace").strip()
    if proc.returncode:
        raise RuntimeError(output or f"reset helper exited {proc.returncode}")
    result = json.loads(output)
    # Opening the same serial again is mandatory; a command exit code alone
    # does not prove successful USB re-enumeration.
    for _ in range(5):
        await asyncio.sleep(1)
        try:
            check = await asyncio.wait_for(hardware_status(force=True), timeout=5)
        except asyncio.TimeoutError:
            continue
        if check.present is True and not check.error and check.serial == st.serial:
            return {**result, "verified": True, "firmware": check.firmware}
    raise RuntimeError("reset sent, but the same HackRF did not reopen; reconnect its USB cable")


maintenance = RadioMaintenance(engine, _reset_hackrf)
_saved_maintenance = toolpaths.load_config().get("radio_maintenance", {})
maintenance.configure(enabled=_saved_maintenance.get("enabled", True),
                      interrupt=_saved_maintenance.get("interrupt", False))


async def hardware_status(force: bool = False) -> rfdetect.HackRfStatus:
    """Probe for a HackRF. Cached briefly so bursts of requests don't
    re-scan USB (SoapySDRUtil --find takes a noticeable moment)."""
    global _hardware
    async with _hardware_lock:
        if _hardware is None or force:
            _hardware = await rfdetect.detect()
    return _hardware


# --------------------------------------------------------------- websocket hub


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def drop(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.drop(ws)


hub = Hub()


@app.on_event("startup")
async def _startup() -> None:
    engine.refresh_tools()
    asyncio.create_task(_pump_logs())
    asyncio.create_task(_pump_progress())
    app.state.maintenance_task = asyncio.create_task(maintenance.run())
    app.state.receiver_task = asyncio.create_task(_pump_receiver())


async def _pump_receiver() -> None:
    while True:
        await asyncio.sleep(0.5)
        if hub.clients:
            await hub.broadcast({"type": "receiver", **receiver.snapshot()})


async def _pump_progress() -> None:
    """Push state while the engine is busy.

    The generator updates progress internally but only emits log lines,
    so without this the progress bar and stage indicator would sit frozen
    for the whole render.
    """
    last = None
    while True:
        await asyncio.sleep(0.5)
        if engine.state == "idle":
            last = None
            continue
        key = (engine.state, engine.progress.get("stage"),
               round(engine.progress.get("seconds", 0), 1))
        if key != last:
            last = key
            snap = engine.snapshot()
            snap["hardware"] = (await hardware_status()).to_dict()
            await hub.broadcast({"type": "state", **snap})


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.receiver_task.cancel()
    with suppress(asyncio.CancelledError):
        await app.state.receiver_task
    await receiver.disconnect()
    app.state.maintenance_task.cancel()
    with suppress(asyncio.CancelledError):
        await app.state.maintenance_task
    await engine.stop(reason="server shutting down")


async def _pump_logs() -> None:
    while True:
        line = await engine.logs.get()
        await hub.broadcast({"type": "log", "line": line})


async def _push_state() -> dict:
    """Snapshot + broadcast, so every client sees transitions immediately."""
    snap = engine.snapshot()
    snap["hardware"] = (await hardware_status()).to_dict()
    await hub.broadcast({"type": "state", **snap})
    return snap


async def _require_hackrf() -> rfdetect.HackRfStatus:
    """Gate RF transmission on radio + generator + transfer + ephemeris.

    Raises 409 naming whichever is actually missing. All of these run
    before the shielded-enclosure confirmation, because reporting 'you
    forgot to tick the box' when the real problem is an unplugged USB
    cable wastes the operator's time.
    """
    st = await hardware_status(force=True)
    engine.refresh_tools()

    if st.present is not True:
        hint = (
            "If a radio IS plugged in, confirm SoapySDR or hackrf_info is "
            "on PATH, and that the HackRF has a WinUSB/libusb driver "
            "installed (Zadig on Windows)."
        )
        raise HTTPException(409, f"{st.label}. Connect the HackRF and check "
                                 f"the USB cable, then retry. {hint}")
    if st.error:
        raise HTTPException(409, st.error)

    if engine.generator is None:
        raise HTTPException(
            409,
            "gps-sdr-sim was not found, so no signal can be generated. Build "
            "it and set GPS_SDR_SIM to the full path of gps-sdr-sim.exe.",
        )
    if engine.transfer is None:
        raise HTTPException(
            409,
            "hackrf_transfer was not found, so nothing can be transmitted. "
            "Install the HackRF host tools (libhackrf) and set "
            "HACKRF_TRANSFER to its full path.",
        )
    if not engine.rinex_files():
        raise HTTPException(
            409,
            "no RINEX navigation file in ./work. Copy a brdc*.22n broadcast "
            "ephemeris from https://cddis.nasa.gov/archive/gnss/data/daily/ "
            "or upload one from the UI.",
        )
    return st


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.add(ws)
    try:
        # First frame carries the hardware preflight, so the UI can say
        # immediately whether a HackRF is attached.
        snap = engine.snapshot()
        snap["hardware"] = (await hardware_status()).to_dict()
        await ws.send_text(json.dumps({"type": "state", **snap}))
        await ws.send_text(json.dumps({"type": "receiver", **receiver.snapshot()}))
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "state":
                snap = engine.snapshot()
                snap["hardware"] = (await hardware_status()).to_dict()
                await ws.send_text(json.dumps({"type": "state", **snap}))
            elif msg.get("type") == "detect":
                snap = engine.snapshot()
                snap["hardware"] = (await hardware_status(force=True)).to_dict()
                await ws.send_text(json.dumps({"type": "state", **snap}))
    except WebSocketDisconnect:
        hub.drop(ws)
    except Exception:
        hub.drop(ws)


# ------------------------------------------------------------------- requests


class ScenarioIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    alt_m: float = 0.0
    duration_s: int = Field(750, ge=1, le=86400)
    sample_rate: int = Field(2_600_000, ge=2_000_000, le=20_000_000)
    iq_bits: Literal[1, 8, 16] = 8
    scenario_time: str | None = None
    freq_hz: int = Field(1_575_420_000)
    tx_gain_db: int = Field(0, ge=0, le=47)
    baseband_bw_hz: int = Field(2_500_000)
    amp_enable: bool = False
    repeat: bool = False
    serial: str | None = None


class StartIn(BaseModel):
    mode: Literal["dry", "rf"] = "dry"
    ephemeris: str | None = None
    shielded: bool = False


class MaintenanceIn(BaseModel):
    enabled: bool = True
    interrupt: bool = False


@app.post("/api/hackrf/reset")
async def reset_hackrf() -> dict:
    try:
        return await maintenance.reset_now()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/hackrf/maintenance")
async def set_maintenance(body: MaintenanceIn) -> dict:
    result = maintenance.configure(**body.model_dump())
    config = toolpaths.load_config()
    config["radio_maintenance"] = body.model_dump()
    toolpaths.save_config(config)
    return result


class LureIn(BaseModel):
    """One-shot: set coordinates and start transmitting in a single call."""

    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    alt_m: float = 0.0
    ephemeris: str | None = None
    shielded: bool = False
    tx_gain_db: int | None = Field(None, ge=0, le=47)
    duration_s: int | None = Field(None, ge=1, le=86400)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def state() -> dict:
    return engine.snapshot()


@app.post("/api/scenario")
async def set_scenario(body: ScenarioIn) -> dict:
    if engine.state != "idle":
        raise HTTPException(409, "stop the running session before editing")
    engine.scenario = Scenario(**body.model_dump())
    return await _push_state()


@app.post("/api/start")
async def start(body: StartIn) -> dict:
    if body.mode == "rf":
        await _require_hackrf()
    try:
        return await engine.start(
            rf=(body.mode == "rf"),
            ephemeris=body.ephemeris,
            shielded=body.shielded,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return await _push_state()


@app.post("/api/stop")
async def stop() -> dict:
    await engine.stop()
    return await _push_state()


@app.get("/api/ephemeris")
async def list_eph() -> dict:
    status = engine.ephemeris_status()
    return {
        **status,
        "epoch": engine.scenario_time(status["recommended"]),
    }


@app.post("/api/ephemeris/upload")
async def upload_eph(file: UploadFile) -> dict:
    name = Path(file.filename or "brdc0010.22n").name
    dest = WORK_DIR / name
    dest.write_bytes(await file.read())
    engine.refresh_tools()
    return {
        "saved": name,
        "files": engine.rinex_files(),
        "epoch": engine.scenario_time(name),
    }


@app.get("/api/capture")
async def capture() -> FileResponse:
    if not engine.iq_path.is_file():
        raise HTTPException(404, "no capture yet - run a dry session first")
    return FileResponse(engine.iq_path, media_type="application/octet-stream",
                        filename=engine.iq_path.name)


@app.get("/api/ports")
async def ports() -> dict:
    return {"ports": available_ports()}


class ReceiverIn(BaseModel):
    port: str = Field(..., min_length=1, max_length=64)
    baud: Literal[4800, 9600, 19200, 38400, 57600, 115200, 230400] = 115200


@app.get("/api/receiver")
async def receiver_state() -> dict:
    return receiver.snapshot()


@app.post("/api/receiver/connect")
async def connect_receiver(body: ReceiverIn) -> dict:
    try:
        return await receiver.connect(body.port, body.baud)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/receiver/disconnect")
async def disconnect_receiver() -> dict:
    return await receiver.disconnect()


@app.post("/api/lure")
async def lure(body: LureIn) -> dict:
    """Type coordinates, hit go, render and transmit. One request."""
    if engine.state != "idle":
        raise HTTPException(409, "a session is already running")

    sc = engine.scenario
    sc.lat, sc.lon, sc.alt_m = body.lat, body.lon, body.alt_m
    if body.tx_gain_db is not None:
        sc.tx_gain_db = body.tx_gain_db
    if body.duration_s is not None:
        sc.duration_s = body.duration_s

    await _require_hackrf()
    await _push_state()
    try:
        return await engine.start(
            rf=True,
            ephemeris=body.ephemeris,
            shielded=body.shielded,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


class ToolsIn(BaseModel):
    name: str
    path: str | None = None


@app.get("/api/tools")
async def tools_status() -> JSONResponse:
    return JSONResponse(toolpaths.status())


@app.post("/api/tools")
async def tools_set(body: ToolsIn) -> JSONResponse:
    if body.name not in toolpaths.TOOLS:
        raise HTTPException(400, f"unknown tool: {body.name}")
    try:
        st = toolpaths.set_tool(body.name, (body.path or "").strip() or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    engine.refresh_tools()
    # A tool change can unblock or block transmission, so re-probe.
    global _hardware
    _hardware = await rfdetect.detect()
    return JSONResponse(st)


@app.get("/api/hardware")
async def hardware(force: int = 0) -> JSONResponse:
    """HackRF preflight. `?force=1` re-scans instead of using the cache."""
    st = await hardware_status(force=bool(force))
    return JSONResponse(st.to_dict())


@app.get("/api/health")
async def health() -> JSONResponse:
    st = await hardware_status()
    return JSONResponse({
        "ok": True,
        "running": engine.state != "idle",
        "rf": engine.snapshot()["rf"],
        "hardware": st.to_dict(),
    })

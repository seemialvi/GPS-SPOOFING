"""Read-only serial observation of NMEA or the ESP32's printed GPS summary."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import math
import re
import time

import serial
from serial.tools import list_ports


def available_ports():
    return [{"port": p.device, "description": p.description, "hwid": p.hwid}
            for p in sorted(list_ports.comports(), key=lambda p: p.device)]


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def coordinate(value, hemisphere):
    raw = number(value)
    if raw is None or raw < 0 or hemisphere not in ("N", "S", "E", "W"):
        return None
    degrees, minutes = divmod(raw, 100)
    result = degrees + minutes / 60
    maximum = 90 if hemisphere in ("N", "S") else 180
    if minutes >= 60 or result > maximum:
        return None
    return -result if hemisphere in ("S", "W") else result


class ReceiverData:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.connected = False
        self.port = None
        self.baud = 115200
        self.error = None
        self.format = None
        self.latitude = self.longitude = self.hdop = self.altitude_m = None
        self.satellites = None
        self.fix_quality = None
        self.utc_time = None
        self.utc_date = None
        self.reported_valid = False
        self.last_position = self.last_line = None
        self.lines = deque(maxlen=200)
        self.satellites_in_view = {}
        self.checksum_errors = 0
        self.sequence = 0
        self.pending = {}

    def _position(self, lat, lon):
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self.reported_valid = False
            return
        self.latitude, self.longitude = lat, lon
        self.last_position = self.clock()
        self.reported_valid = True

    def feed(self, line):
        line = line.strip()[:1024]
        if not line:
            return
        self.last_line = self.clock()
        self.sequence += 1
        self.lines.append({"seq": self.sequence,
                           "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                           "text": line})
        if line.startswith("$"):
            try:
                body, checksum = line[1:].rsplit("*", 1)
                actual = 0
                for char in body:
                    actual ^= ord(char)
                if len(checksum) != 2 or actual != int(checksum, 16):
                    raise ValueError("checksum")
            except ValueError:
                self.checksum_errors += 1
                return
            self.format = "NMEA"
            fields = body.split(",")
            kind = fields[0][-3:]
            try:
                if kind == "GGA":
                    self.utc_time = fields[1] or None
                    self.fix_quality = int(fields[6] or 0)
                    self.satellites = int(fields[7] or 0)
                    self.hdop = number(fields[8])
                    self.altitude_m = number(fields[9])
                    if self.fix_quality > 0:
                        self._position(coordinate(fields[2], fields[3]), coordinate(fields[4], fields[5]))
                    else:
                        self.reported_valid = False
                elif kind == "RMC":
                    self.utc_time = fields[1] or None
                    self.utc_date = fields[9] or None
                    if fields[2] == "A":
                        self._position(coordinate(fields[3], fields[4]), coordinate(fields[5], fields[6]))
                    else:
                        self.reported_valid = False
                elif kind == "GSA":
                    if int(fields[2] or 1) == 1:
                        self.reported_valid = False
                    if len(fields) > 16:
                        self.hdop = number(fields[16])
                elif kind == "GSV":
                    talker = fields[0][:-3]
                    if fields[2] == "1":
                        self.satellites_in_view[talker] = {}
                    target = self.satellites_in_view.setdefault(talker, {})
                    for index in range(4, len(fields)-3, 4):
                        if fields[index]:
                            target[fields[index]] = {"talker": talker, "prn": fields[index],
                                "elevation": number(fields[index+1]), "azimuth": number(fields[index+2]),
                                "cno": number(fields[index+3]), "seen": self.clock()}
            except (ValueError, IndexError):
                # Keep malformed sentences visible without corrupting a previous fix.
                return
            return

        if "no fresh gps position" in line.lower() or "location: invalid" in line.lower():
            self.format = "ESP32 text"
            self.reported_valid = False
            self.pending.clear()
            return
        match = re.fullmatch(r"(Latitude|Longitude|Satellites|HDOP)\s*:\s*([-+\d.eE]+)", line, re.I)
        if not match:
            return
        self.format = "ESP32 text"
        key, value = match.group(1).lower(), number(match.group(2))
        if value is None:
            return
        if key == "latitude":
            self.pending = {"latitude": value}
        elif key == "longitude":
            self.pending["longitude"] = value
        elif key == "satellites":
            self.satellites = int(value)
            if self.satellites <= 0:
                self.reported_valid = False
                self.pending.clear()
        elif key == "hdop":
            self.hdop = value
            if value >= 99 or not self.satellites:
                self.reported_valid = False
            elif {"latitude", "longitude"} <= self.pending.keys():
                self._position(self.pending["latitude"], self.pending["longitude"])
            self.pending.clear()

    def snapshot(self):
        now = self.clock()
        age = None if self.last_position is None else max(0, now-self.last_position)
        line_age = None if self.last_line is None else max(0, now-self.last_line)
        fresh = self.connected and self.reported_valid and age is not None and age <= 5
        if not self.connected:
            status = "Disconnected"
        elif line_age is None:
            status = "Connected / waiting for serial data"
        elif fresh:
            status = "Fresh reported fix"
        elif self.reported_valid:
            status = "Stale position / no fresh fix"
        else:
            status = "No fresh GPS position"
        return {"connected": self.connected, "port": self.port, "baud": self.baud,
                "error": self.error, "status": status, "format": self.format,
                "fresh": fresh, "latitude": self.latitude if fresh else None,
                "longitude": self.longitude if fresh else None,
                "last_position": {"latitude": self.latitude, "longitude": self.longitude},
                "position_age_s": age, "serial_age_s": line_age,
                "satellites": self.satellites, "hdop": self.hdop, "altitude_m": self.altitude_m,
                "fix_quality": self.fix_quality, "utc_time": self.utc_time, "utc_date": self.utc_date,
                "checksum_errors": self.checksum_errors, "lines": list(self.lines),
                "satellites_in_view": [{k: v for k, v in row.items() if k != "seen"}
                    for group in self.satellites_in_view.values() for row in group.values()
                    if now-row["seen"] <= 10]}


class ReceiverMonitor:
    def __init__(self):
        self.data = ReceiverData()
        self.serial = None
        self.task = None
        self.lock = asyncio.Lock()

    def snapshot(self):
        return self.data.snapshot()

    async def connect(self, port, baud):
        async with self.lock:
            if self.serial is not None:
                raise ValueError("disconnect the current receiver port first")
            if port not in {p["port"] for p in available_ports()}:
                raise ValueError("select a currently available serial port")
            device = serial.Serial(port=None, baudrate=baud, timeout=0.2, write_timeout=0.2)
            device.dtr = device.rts = False
            device.port = port
            try:
                await asyncio.to_thread(device.open)
            except (serial.SerialException, OSError) as exc:
                device.close()
                self.data.error = f"Cannot open {port}. Close Arduino Serial Monitor or other apps using this port. {exc}"
                raise ValueError(self.data.error) from exc
            self.data = ReceiverData()
            self.data.connected, self.data.port, self.data.baud = True, port, baud
            self.serial = device
            self.task = asyncio.create_task(self._read(device))
            return self.snapshot()

    async def _read(self, device):
        buffer = b""
        try:
            while device.is_open:
                chunk = await asyncio.to_thread(device.read, 256)
                if not chunk:
                    continue
                buffer += chunk.replace(b"\r", b"\n")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self.data.feed(line.decode("utf-8", errors="replace"))
                if len(buffer) > 4096:
                    self.data.feed(buffer[:1024].decode("utf-8", errors="replace"))
                    buffer = b""
        except (serial.SerialException, OSError, AttributeError, TypeError) as exc:
            if self.serial is device:
                self.data.error = f"Receiver serial connection lost: {exc}"
        finally:
            device.close()
            if self.serial is device:
                self.serial = None
            self.data.connected = False

    async def disconnect(self):
        async with self.lock:
            device, task = self.serial, self.task
            self.serial = None
            if device is not None:
                # Closing our own handle ends the bounded read; no other app is touched.
                device.close()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            self.serial = self.task = None
            self.data.connected = False
            return self.snapshot()

import asyncio
import queue
import unittest
from unittest.mock import patch

from receiver_monitor import ReceiverData, ReceiverMonitor


def nmea(body):
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}"


TEXT_FIX = "Latitude: 31.451852\nLongitude: 74.291959\nSatellites: 4\nHDOP: 2.66\n"


class ReceiverParserTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.data = ReceiverData(clock=lambda: self.now)
        self.data.connected = True

    def feed(self, text):
        for line in text.splitlines():
            self.data.feed(line)

    def test_user_text_then_loss_does_not_display_stale_coordinates(self):
        self.feed(TEXT_FIX)
        snap = self.data.snapshot()
        self.assertTrue(snap["fresh"])
        self.assertAlmostEqual(snap["latitude"],31.451852)
        self.assertEqual(snap["satellites"],4)
        self.feed("No fresh GPS position\nSatellites: 0\nHDOP: 99.99")
        snap = self.data.snapshot()
        self.assertFalse(snap["fresh"])
        self.assertIsNone(snap["latitude"])
        self.assertEqual(snap["last_position"]["latitude"],31.451852)
        self.assertEqual(snap["hdop"],99.99)

    def test_serial_noise_does_not_refresh_old_fix(self):
        self.feed(TEXT_FIX)
        self.now = 6
        self.feed("ESP32 still running")
        self.assertFalse(self.data.snapshot()["fresh"])
        self.assertEqual(self.data.snapshot()["position_age_s"],6)

    def test_nmea_gga_and_zero_fix(self):
        self.feed(nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"))
        snap = self.data.snapshot()
        self.assertAlmostEqual(snap["latitude"],48.1173)
        self.assertAlmostEqual(snap["longitude"],11.5166666667)
        self.assertEqual(snap["satellites"],8)
        self.assertEqual(snap["utc_time"],"123519")
        self.feed(nmea("GPGGA,123520,,,,,0,00,99.99,,,,,,"))
        self.assertFalse(self.data.snapshot()["fresh"])

    def test_rmc_south_west_and_void(self):
        self.feed(nmea("GPRMC,123519,A,3352.128,S,15112.558,W,0,0,191026,,,A"))
        self.assertAlmostEqual(self.data.snapshot()["latitude"],-33.8688)
        self.assertAlmostEqual(self.data.snapshot()["longitude"],-151.2093)
        self.feed(nmea("GPRMC,123520,V,,,,,,,191026,,,N"))
        self.assertFalse(self.data.snapshot()["fresh"])

    def test_bad_checksum_is_visible_but_not_a_fix(self):
        self.feed("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*00")
        self.assertEqual(self.data.snapshot()["checksum_errors"],1)
        self.assertFalse(self.data.snapshot()["fresh"])
        self.assertEqual(len(self.data.snapshot()["lines"]),1)

    def test_gsv_is_separate_from_satellites_used_and_expires(self):
        self.feed(nmea("GPGSV,1,1,02,01,40,083,42,03,17,308,"))
        snap = self.data.snapshot()
        self.assertEqual(len(snap["satellites_in_view"]),2)
        self.assertEqual(snap["satellites_in_view"][0]["cno"],42)
        self.assertIsNone(snap["satellites"])
        self.now = 11
        self.assertEqual(self.data.snapshot()["satellites_in_view"],[])

    def test_log_is_bounded_and_zero_coordinate_is_valid(self):
        self.feed(TEXT_FIX.replace("31.451852","0").replace("74.291959","0"))
        self.assertEqual(self.data.snapshot()["latitude"],0)
        for index in range(300):
            self.data.feed(str(index))
        self.assertEqual(len(self.data.snapshot()["lines"]),200)


class FakeSerial:
    def __init__(self, **kwargs):
        self.is_open = False
        self.port = None
        self.baudrate = kwargs["baudrate"]
        self.chunks = queue.Queue()

    def open(self):
        if self.dtr or self.rts:
            raise AssertionError("monitor must not deliberately assert reset lines")
        self.is_open = True

    def read(self, size):
        try:
            return self.chunks.get(timeout=0.03)
        except queue.Empty:
            return b""

    def close(self):
        self.is_open = False

    def write(self, data):
        raise AssertionError("receiver monitoring must be read-only")


class ReceiverLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_serial_and_disconnect(self):
        monitor = ReceiverMonitor()
        device = FakeSerial(baudrate=115200)
        with patch("receiver_monitor.available_ports", return_value=[{"port":"COM13"}]), \
             patch("receiver_monitor.serial.Serial", return_value=device):
            await monitor.connect("COM13",115200)
            try:
                device.chunks.put(b"Lati")
                device.chunks.put(TEXT_FIX[4:].replace("\n","\r\n").encode())
                for _ in range(50):
                    if monitor.snapshot()["fresh"]:
                        break
                    await asyncio.sleep(.02)
                self.assertTrue(monitor.snapshot()["fresh"])
                with self.assertRaisesRegex(ValueError,"disconnect"):
                    await monitor.connect("COM13",115200)
            finally:
                await monitor.disconnect()
            self.assertFalse(monitor.snapshot()["connected"])
            self.assertFalse(monitor.snapshot()["fresh"])
            self.assertIsNone(monitor.task)

    async def test_busy_port_has_actionable_error(self):
        monitor = ReceiverMonitor()
        device = FakeSerial(baudrate=115200)
        device.open = lambda: (_ for _ in ()).throw(OSError("Access is denied"))
        with patch("receiver_monitor.available_ports", return_value=[{"port":"COM13"}]), \
             patch("receiver_monitor.serial.Serial", return_value=device):
            with self.assertRaisesRegex(ValueError,"Close Arduino Serial Monitor"):
                await monitor.connect("COM13",115200)
        self.assertFalse(monitor.snapshot()["connected"])

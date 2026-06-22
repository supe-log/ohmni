"""RPLidar A2M8 express-scan reader via ADB serial.

Port of control-app/lidar_reader.js:
- Spawns `adb shell cat /dev/ttyUSB0` subprocess
- Decodes 84-byte express-scan packets (4 header + 80 cabin data)
- Extracts angle/distance pairs from 16 cabins per packet
- Converts to (x, y) points in robot frame
- Filters robot body signature (~182mm band)
- Emits complete 360-degree scans at ~11 Hz
"""

import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from dimos.utils.logging_config import setup_logger

from .types import OhmniConfig

logger = setup_logger()

PACKET_SIZE = 84  # Express scan: 4 header + 80 cabin data
SYNC1 = 0xA
SYNC2 = 0x5


class OhmniLidarReader:
    """Direct RPLidar A2M8 express-scan reader over ADB.

    Reads raw serial data from /dev/ttyUSB0 through ADB, decodes
    express-scan packets, and emits per-revolution scan data.

    The on-device Node process must start the LiDAR motor and express
    scan first (via bot_shell 'start_collision_detection'). This reader
    then opens the serial port concurrently to capture the data stream.
    """

    def __init__(
        self,
        adb_addr: str,
        config: OhmniConfig | None = None,
        on_scan: "callable | None" = None,
        bridge: "OhmniBridge | None" = None,
    ) -> None:
        self._adb_addr = adb_addr
        self._config = config or OhmniConfig()
        self._on_scan = on_scan
        self._bridge = bridge  # Used to start motor via bot_shell

        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        # Packet decoding state
        self._buffer = bytearray()
        self._prev_packet: bytes | None = None
        self._prev_start_angle: float = -1.0
        self._current_scan: list[dict] = []
        self._last_start_angle: float = -1.0

        # Latest complete scan
        self.last_scan: list[dict] = []
        self.scan_count = 0

        # Optional replay capture: set OHMNI_LIDAR_REPLAY=/path/to/file.jsonl
        # to dump every parsed scan to disk for offline replay.
        self._replay_fp = None
        replay_path = os.environ.get("OHMNI_LIDAR_REPLAY")
        if replay_path:
            try:
                Path(replay_path).parent.mkdir(parents=True, exist_ok=True)
                self._replay_fp = open(replay_path, "a")  # noqa: SIM115
                logger.info("lidar replay capture -> %s", replay_path)
            except OSError as e:
                logger.warning("lidar replay open failed: %s", e)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True, name="ohmni-lidar"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._process:
            try:
                self._process.kill()
            except OSError:
                pass
            self._process = None
        if self._thread:
            self._thread.join(timeout=3)
        if self._replay_fp is not None:
            try:
                self._replay_fp.close()
            except OSError:
                pass
            self._replay_fp = None

    def get_latest_scan(self) -> list[dict]:
        return self.last_scan

    def get_filtered_scan(self) -> list[dict]:
        return [
            r
            for r in self.last_scan
            if self._config.lidar_min_distance
            <= r["distance"]
            <= self._config.lidar_max_distance
        ]

    def get_points_xy(self) -> np.ndarray:
        """Convert latest filtered scan to Nx2 array of (x, y) in meters.

        Coordinate frame: x=forward, y=left (robot frame).
        LiDAR is front-mounted: 0°=front, 90°=left, 180°=back, 270°=right.
        """
        scan = self.get_filtered_scan()
        if not scan:
            return np.empty((0, 2), dtype=np.float32)

        points = np.empty((len(scan), 2), dtype=np.float32)
        for i, r in enumerate(scan):
            angle_rad = math.radians(r["angle"])
            dist_m = r["distance"] / 1000.0
            # 0° = forward (x+), 90° = left (y+)
            points[i, 0] = dist_m * math.cos(angle_rad)
            points[i, 1] = dist_m * math.sin(angle_rad)
        return points

    # --- Internal ---

    def _ensure_motor_running(self) -> None:
        """Initialize the lidar via bot_shell (patched bot_shell_lidar.js).

        On this firmware, the on-device collision-detection node owns
        `/dev/ttyUSB0` exclusively, so we cannot read raw serial in
        parallel. Instead we ask bot_shell to scan + start CD, then
        poll `lidar_get_scan` for one revolution at a time.

        Requires the dimos patch in
        `control-app/bot_shell_lidar_dimos.js` to be installed at
        `/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/bot_shell_lidar.js`
        on the robot.
        """
        if not self._bridge:
            logger.warning("LiDAR has no bridge; cannot start via bot_shell")
            return
        logger.info("Starting LiDAR via bot_shell (scan + collision detection)...")
        # 1. Populate _botnode._lidarDevice (opens /dev/ttyUSB0 async).
        self._bridge.send_command("scan_lidar_device")
        time.sleep(2)
        # 2. Create collision-detection node (uses _lidarDevice). Its
        #    .start() calls start_express_scan() then sets motor PWM
        #    with a 300ms delay.
        self._bridge.send_command("start_collision_detection")
        time.sleep(2)
        # 3. Defensive: explicitly kick motor PWM and start_express_scan
        #    in case cd.start raced the device's async open. These are
        #    no-ops if already running.
        self._bridge.send_command("lidar_set_pwm 660")
        time.sleep(0.5)
        self._bridge.send_command("lidar_scan")
        # Give the lidar a couple of revolutions to fill cabin buffers.
        time.sleep(3)
        logger.info("LiDAR motor started")

    def _read_loop(self) -> None:
        self._ensure_motor_running()
        if not self._bridge:
            return
        while self._running:
            try:
                self._poll_one_scan()
            except Exception as e:
                logger.warning("LiDAR poll error: %s", e)
                time.sleep(0.5)

    _poll_count: int = 0

    def _poll_one_scan(self) -> None:
        """Ask bot_shell for one revolution of cabin events.

        Uses `lidar_get_scan_v2` (independent JS decoder in our patched
        bot_shell) instead of `lidar_get_scan` (subscribes to
        `new_cabin` events, which the on-device parser sometimes wedges
        and stops emitting after the first run).
        """
        resp = self._bridge.send_command("lidar_get_scan_v2", timeout=1.5)
        self._poll_count += 1
        if self._poll_count <= 5 or self._poll_count % 50 == 1:
            preview = (resp[:80] + "...") if resp and len(resp) > 80 else resp
            logger.info("lidar poll #%d resp: %r", self._poll_count, preview)
        if not resp:
            return
        line = next(
            (l for l in resp.splitlines()
             if l.startswith("SCAN:") or l.startswith("SCAN_EMPTY")),
            "",
        )
        if not line or line.startswith("SCAN_EMPTY") or line.startswith("SCAN_ERR"):
            return
        scan_part = line.split("|", 1)[0]
        readings_str = scan_part[len("SCAN:"):]
        readings: list[dict] = []
        for tok in readings_str.split(","):
            if not tok:
                continue
            try:
                a_str, d_str = tok.split(":", 1)
                ang = float(a_str)
                dist = int(d_str)
            except ValueError:
                continue
            if dist <= 0 or self._is_body(dist):
                continue
            readings.append({"angle": ang, "distance": dist})
        if not readings:
            if self._poll_count % 50 == 1:
                logger.info("lidar parsed but 0 readings after filter")
            return
        self.last_scan = readings
        self.scan_count += 1
        if self.scan_count <= 3 or self.scan_count % 25 == 0:
            logger.info("lidar parsed scan #%d: %d readings", self.scan_count, len(readings))
        if self._replay_fp is not None:
            try:
                self._replay_fp.write(
                    json.dumps({
                        "ts": time.time(),
                        "n": self.scan_count,
                        "readings": readings,
                    }) + "\n"
                )
                self._replay_fp.flush()
            except OSError:
                pass
        if self._on_scan:
            try:
                self._on_scan(readings)
            except Exception as e:
                logger.warning("Scan callback error: %s", e)

    def _spawn_and_read_LEGACY(self) -> None:
        # Retained for reference. Direct serial requires releasing the
        # on-device collision detection node first; not used in the
        # bot_shell-poll path.
        self._process = subprocess.Popen(
            [
                "adb", "-s", self._adb_addr, "exec-out",
                f"cat {self._config.lidar_serial_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        try:
            while self._running and self._process.poll() is None:
                chunk = self._process.stdout.read(4096)
                if not chunk:
                    break
                self._on_data(chunk)
        finally:
            if self._process:
                try:
                    self._process.kill()
                except OSError:
                    pass
                self._process = None

    def _on_data(self, data: bytes) -> None:
        """Process raw serial data, extract and decode packets."""
        self._buffer.extend(data)

        while len(self._buffer) >= PACKET_SIZE:
            # Check sync header
            sync1 = (self._buffer[0] & 0xF0) >> 4
            sync2 = (self._buffer[1] & 0xF0) >> 4

            if sync1 != SYNC1 or sync2 != SYNC2:
                # Scan forward for sync
                found = False
                for i in range(1, len(self._buffer) - 1):
                    if (
                        ((self._buffer[i] & 0xF0) >> 4) == SYNC1
                        and ((self._buffer[i + 1] & 0xF0) >> 4) == SYNC2
                    ):
                        self._buffer = self._buffer[i:]
                        found = True
                        break
                if not found:
                    self._buffer = self._buffer[-1:]
                continue

            if len(self._buffer) < PACKET_SIZE:
                break

            # Verify checksum
            rcv_cs = (self._buffer[0] & 0x0F) | ((self._buffer[1] & 0x0F) << 4)
            calc_cs = 0
            for i in range(2, PACKET_SIZE):
                calc_cs ^= self._buffer[i]

            if rcv_cs != calc_cs:
                self._buffer = self._buffer[2:]
                continue

            # Valid packet
            packet = bytes(self._buffer[:PACKET_SIZE])
            self._buffer = self._buffer[PACKET_SIZE:]
            self._process_packet(packet)

        # Prevent unbounded growth
        if len(self._buffer) > PACKET_SIZE * 100:
            self._buffer = self._buffer[-(PACKET_SIZE * 10) :]

    def _process_packet(self, packet: bytes) -> None:
        """Decode one express-scan packet into angle/distance readings."""
        # Extract start angle (q6 format)
        start_angle_q6 = packet[2] | ((packet[3] & 0x7F) << 8)
        start_angle = start_angle_q6 / 64.0

        if self._prev_packet is None:
            self._prev_packet = packet
            self._prev_start_angle = start_angle
            return

        prev_packet = self._prev_packet
        prev_start_angle = self._prev_start_angle
        next_start_angle = start_angle

        # Angle difference between packets
        angle_diff = next_start_angle - prev_start_angle
        if angle_diff < 0:
            angle_diff += 360

        # Process 16 cabins from the PREVIOUS packet
        readings = []
        for cabin_idx in range(16):
            offset = 4 + cabin_idx * 5

            # Distance values (14-bit, in mm)
            d1 = ((prev_packet[offset] & 0xFC) >> 2) | (prev_packet[offset + 1] << 6)
            d2 = (
                (prev_packet[offset + 2] & 0xFC) >> 2
            ) | (prev_packet[offset + 3] << 6)

            # Delta angle (q3 format with sign)
            theta1_q3 = ((prev_packet[offset] & 0x03) << 4) | (
                prev_packet[offset + 4] & 0x0F
            )
            theta1 = (theta1_q3 & 0x1F) / 8.0
            if theta1_q3 >> 5:
                theta1 = -theta1

            theta2_q3 = ((prev_packet[offset + 2] & 0x03) << 4) | (
                (prev_packet[offset + 4] & 0xF0) >> 4
            )
            theta2 = (theta2_q3 & 0x1F) / 8.0
            if theta2_q3 >> 5:
                theta2 = -theta2

            # Interpolated angles within this packet's angular span
            k = cabin_idx
            angle_interp1 = angle_diff * (k * 2) / 32.0
            angle_interp2 = angle_diff * (k * 2 + 1) / 32.0

            angle1 = prev_start_angle + angle_interp1 - theta1
            angle2 = prev_start_angle + angle_interp2 - theta2

            # Normalize to [0, 360)
            angle1 = angle1 % 360
            angle2 = angle2 % 360

            # Filter: valid distance and not robot body signature
            if d1 > 0 and not self._is_body(d1):
                readings.append({"angle": round(angle1, 1), "distance": d1})
            if d2 > 0 and not self._is_body(d2):
                readings.append({"angle": round(angle2, 1), "distance": d2})

        # Detect revolution boundary
        if prev_start_angle > 270 and start_angle < 90:
            if len(self._current_scan) > 10:
                self.last_scan = self._current_scan
                self.scan_count += 1
                if self._on_scan:
                    try:
                        self._on_scan(self.last_scan)
                    except Exception as e:
                        logger.warning("Scan callback error: %s", e)
            self._current_scan = []

        self._current_scan.extend(readings)
        self._prev_packet = packet
        self._prev_start_angle = start_angle

    def _is_body(self, distance: int) -> bool:
        """Filter the robot's own body signature (~182mm band)."""
        return (
            self._config.lidar_body_dist_min
            <= distance
            <= self._config.lidar_body_dist_max
        )

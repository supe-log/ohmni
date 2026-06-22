"""Drive train adapter: Twist -> bot_shell motor commands.

Converts dimos Twist messages (SI units: m/s, rad/s) to Ohmni
bot_shell commands (mm/s, deg/s).

Bot_shell drive commands (from server.js and PROTOCOL_ANALYSIS.md):
- manual_move <linear_mm> <angular_deg>  — continuous velocity
- pre_drive <distance_mm> <speed>        — drive a set distance
- pre_rot <angle_deg> <speed>            — rotate a set angle
"""

import math
import time
import threading

from dimos.utils.logging_config import setup_logger

from .bridge import OhmniBridge

logger = setup_logger()

# Rate limit: don't flood bot_shell with move commands
MIN_CMD_INTERVAL = 0.1  # 10 Hz max


class OhmniDriveTrain:
    """Translates Twist velocity commands to Ohmni bot_shell motor commands."""

    def __init__(self, bridge: OhmniBridge) -> None:
        self._bridge = bridge
        self._last_cmd_time = 0.0
        self._lock = threading.Lock()
        self._cmd_count = 0
        self._last_log_count = -1

    def move_twist(self, linear_x: float, angular_z: float) -> None:
        """Send a velocity command.

        Args:
            linear_x: Forward velocity in m/s (positive = forward)
            angular_z: Rotational velocity in rad/s (positive = left)
        """
        now = time.monotonic()
        with self._lock:
            if now - self._last_cmd_time < MIN_CMD_INTERVAL:
                return
            self._last_cmd_time = now
            self._cmd_count += 1

        linear_mm = int(linear_x * 1000)
        angular_deg = int(angular_z * 180 / math.pi)
        # Log first 5 commands, then every 25th, plus any non-zero
        # transition for visibility into what the planner is sending.
        is_zero = (linear_mm == 0 and angular_deg == 0)
        should_log = (
            self._cmd_count <= 5
            or self._cmd_count % 25 == 0
            or (not is_zero and self._cmd_count - self._last_log_count > 1)
        )
        if should_log:
            logger.info(
                "drive cmd #%d: linear=%dmm/s angular=%ddeg/s",
                self._cmd_count, linear_mm, angular_deg,
            )
            self._last_log_count = self._cmd_count
        self._bridge.send_command(f"manual_move {linear_mm} {angular_deg}")

    def drive_distance(self, distance_mm: int, speed: int = 8) -> str:
        """Drive a set distance.

        Args:
            distance_mm: Distance in mm (positive = forward)
            speed: Speed setting (1-20, typical 8)
        """
        return self._bridge.send_command(f"pre_drive {distance_mm} {speed}")

    def rotate(self, angle_deg: int, speed: int = 8) -> str:
        """Rotate by a set angle.

        Args:
            angle_deg: Angle in degrees (positive = left)
            speed: Speed setting (1-20, typical 8)
        """
        return self._bridge.send_command(f"pre_rot {angle_deg} {speed}")

    def stop(self) -> None:
        """Emergency stop — zero velocity."""
        self._bridge.send_command("manual_move 0 0")

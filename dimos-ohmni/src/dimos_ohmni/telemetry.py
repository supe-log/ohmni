"""Telemetry: battery, head pose, odometry, LED control.

Reads telemetry from the Ohmni via bot_shell commands and
exposes structured data for dimos streams.

Bot_shell commands used:
- battery          -> battery level/charging state
- neck_angle <val> -> set neck, track last sent value
- wake_head / rest_head / sleep_head
- light_color <duration> <hue> <sat> <val> -> LED ring
"""

import logging
import re
import threading
import time

from .bridge import OhmniBridge
from .types import OhmniBatteryStatus, OhmniHeadState

logger = logging.getLogger(__name__)


class OhmniTelemetry:
    """Polls and manages robot telemetry."""

    def __init__(self, bridge: OhmniBridge) -> None:
        self._bridge = bridge
        self._running = False
        self._thread: threading.Thread | None = None

        # State
        self.battery = OhmniBatteryStatus()
        self.head = OhmniHeadState()

        # Callbacks
        self._on_battery: "callable | None" = None
        self._on_head: "callable | None" = None

    def start(
        self,
        on_battery: "callable | None" = None,
        on_head: "callable | None" = None,
        poll_interval: float = 10.0,
    ) -> None:
        self._on_battery = on_battery
        self._on_head = on_head
        self._running = True
        self._poll_interval = poll_interval
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="ohmni-telemetry"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # --- Head control ---

    def wake_head(self) -> str:
        """Activate the neck servo."""
        self.head.awake = True
        return self._bridge.send_command("wake_head")

    def rest_head(self) -> str:
        """Deactivate the neck servo (relaxed position)."""
        self.head.awake = False
        return self._bridge.send_command("rest_head")

    def set_neck_angle(self, angle: int) -> str:
        """Set neck angle. 400=down, 512=center, 600=up."""
        angle = max(400, min(600, angle))
        self.head.angle = angle
        result = self._bridge.send_command(f"neck_angle {angle}")
        if self._on_head:
            self._on_head(self.head)
        return result

    # --- LED control ---

    def set_led(self, duration: int, hue: int, sat: int = 255, val: int = 255) -> str:
        """Set LED ring color (HSV).

        Args:
            duration: Duration in 0.1s units (e.g. 20 = 2 seconds)
            hue: Hue 0-255
            sat: Saturation 0-255
            val: Value/brightness 0-255
        """
        return self._bridge.send_command(
            f"light_color {duration} {hue} {sat} {val}"
        )

    # --- Battery ---

    def get_battery(self) -> OhmniBatteryStatus:
        """Alias for poll_battery — public name for skill use."""
        return self.poll_battery()

    # --- Wheel odometry ---

    def read_wheel_apos(self) -> tuple[int | None, int | None]:
        """Read absolute servo positions for left (sid 0) and right (sid 1)
        wheels. Returns (left, right) raw encoder counts, or None on parse
        failure.
        """
        resp = self._bridge.send_command("apos 0", timeout=0.5)
        m_left = re.search(r"apos\s*0\s*=\s*(-?\d+)", resp)
        resp = self._bridge.send_command("apos 1", timeout=0.5)
        m_right = re.search(r"apos\s*1\s*=\s*(-?\d+)", resp)
        return (
            int(m_left.group(1)) if m_left else None,
            int(m_right.group(1)) if m_right else None,
        )

    def poll_battery(self) -> OhmniBatteryStatus:
        """Read battery status from bot_shell."""
        raw = self._bridge.send_command("battery", timeout=2.0)
        self.battery.raw = raw

        # Parse battery response — Ohmni format:
        # "Last battery level: 50\nLast cell voltages: [...]\nLast docked: 0"
        level_match = re.search(r"battery level:\s*(\d+)", raw, re.IGNORECASE)
        if level_match:
            self.battery.level = float(level_match.group(1))
        else:
            # Fallback: try percent format
            level_match = re.search(r"(\d+)%", raw)
            if level_match:
                self.battery.level = float(level_match.group(1))

        # Check docked/charging status
        docked_match = re.search(r"docked:\s*(\d+)", raw, re.IGNORECASE)
        self.battery.charging = bool(docked_match and docked_match.group(1) != "0")

        return self.battery

    # --- Polling loop ---

    def _poll_loop(self) -> None:
        while self._running:
            try:
                status = self.poll_battery()
                if self._on_battery:
                    self._on_battery(status)
            except Exception as e:
                logger.warning("Telemetry poll error: %s", e)
            time.sleep(self._poll_interval)

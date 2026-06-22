"""ADB + bot_shell bridge for the Ohmni robot.

Python port of control-app/server.js (lines 19-200):
- ADB WiFi connection management with auto-reconnect
- bot_shell Unix socket forwarding and persistent TCP connection
- Command send/receive with response callback and timeout
- Camera detection by V4L2 card name
"""

from dimos.utils.logging_config import setup_logger
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .types import OhmniConfig

logger = setup_logger()


@dataclass
class CameraDevice:
    dev: str  # e.g. /dev/video0
    resolution: str  # e.g. 640x480
    name: str  # e.g. "Screen Camera"


# Camera card name matching — device numbers change after USB re-enumeration
CAMERA_SPECS = {
    0: {"match": "See3CAM", "res": "320x240", "name": "Screen Camera"},
    1: {"match": "HD USB Camera", "res": "320x240", "name": "Floor Camera"},
}


class OhmniBridge:
    """Manages the ADB connection and bot_shell TCP socket to the Ohmni robot.

    Mirrors the proven approach from control-app/server.js:
    - ADB WiFi connect + root + port forward
    - Persistent TCP connection to the forwarded bot_shell socket
    - Command/response protocol with timeout
    """

    def __init__(self, config: OhmniConfig | None = None) -> None:
        self.config = config or OhmniConfig()
        self._adb_addr = f"{self.config.ip}:{self.config.adb_port}"

        # Bot shell state
        self._sock: socket.socket | None = None
        self._sock_lock = threading.Lock()
        self._ready = False
        self._recv_buffer = ""
        self._response_event = threading.Event()
        self._response_data = ""
        self._recv_thread: threading.Thread | None = None

        # ADB state
        self._adb_connected = False
        self._adb_thread: threading.Thread | None = None
        self._running = False

        # Detected cameras
        self.cameras: dict[int, CameraDevice] = {}

    def connect(self) -> None:
        """Start ADB monitoring and connect to bot_shell."""
        self._running = True
        self._ensure_adb()
        self._adb_thread = threading.Thread(
            target=self._adb_monitor_loop, daemon=True, name="ohmni-adb-monitor"
        )
        self._adb_thread.start()

    def disconnect(self) -> None:
        """Disconnect from the robot."""
        self._running = False
        self._close_socket()
        if self._adb_thread:
            self._adb_thread.join(timeout=5)

    @property
    def is_ready(self) -> bool:
        return self._ready and self._sock is not None

    # --- Command interface ---

    def send_command(self, cmd: str, timeout: float = 1.0) -> str:
        """Send a command to bot_shell and return the response.

        Args:
            cmd: The bot_shell command (e.g. "neck_angle 512", "battery")
            timeout: Max seconds to wait for response

        Returns:
            Response string from bot_shell, or error message
        """
        with self._sock_lock:
            if not self._sock or not self._ready:
                self._connect_bot_shell()
                if not self._ready:
                    return "error: bot_shell not connected"

            self._response_data = ""
            self._response_event.clear()

            try:
                self._sock.sendall((cmd + "\n").encode())
            except (OSError, BrokenPipeError) as e:
                logger.warning("bot_shell send failed: %s", e)
                self._close_socket()
                return f"error: send failed: {e}"

        # Wait for response (outside lock so recv thread can write)
        if self._response_event.wait(timeout):
            return self._response_data
        return self._response_data or "command sent"

    # --- ADB management ---

    def _adb_cmd(self, args: list[str], timeout: float = 5.0) -> tuple[int, str]:
        """Run an ADB command and return (returncode, stdout)."""
        try:
            result = subprocess.run(
                ["adb", "-s", self._adb_addr] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        except FileNotFoundError:
            return -1, "adb not found"

    def _check_adb(self) -> bool:
        """Check if ADB is connected to the robot."""
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return (
                self._adb_addr in result.stdout and "device" in result.stdout
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _ensure_adb(self) -> bool:
        """Ensure ADB is connected, rooted, and port-forwarded."""
        if self._check_adb():
            if not self._adb_connected:
                self._adb_connected = True
                logger.info("ADB connected to %s", self._adb_addr)
                self._setup_adb()
            return True

        logger.info("ADB not connected, attempting connect to %s", self._adb_addr)
        try:
            result = subprocess.run(
                ["adb", "connect", self._adb_addr],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "connected" in result.stdout and "unable" not in result.stdout:
                self._adb_connected = True
                logger.info("ADB reconnected to %s", self._adb_addr)
                self._setup_adb()
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        self._adb_connected = False
        logger.warning("ADB connect failed")
        return False

    def _setup_adb(self) -> None:
        """Root, set up port forward, detect cameras."""
        # Root access (needed for camera devices)
        self._adb_cmd(["root"], timeout=3)
        time.sleep(1)
        # Re-connect after root (ADB restarts)
        subprocess.run(
            ["adb", "connect", self._adb_addr],
            capture_output=True,
            text=True,
            timeout=5,
        )
        time.sleep(0.5)

        # Port forward for bot_shell
        rc, _ = self._adb_cmd([
            "forward",
            f"tcp:{self.config.bot_shell_local_port}",
            f"localfilesystem:{self.config.bot_shell_sock_path}",
        ])
        if rc == 0:
            logger.info("ADB port forward established for bot_shell")

        # Detect cameras
        self.detect_cameras()

        # Connect to bot_shell
        self._connect_bot_shell()

    def _adb_monitor_loop(self) -> None:
        """Periodically check ADB connection."""
        while self._running:
            self._ensure_adb()
            time.sleep(self.config.adb_reconnect_interval)

    # --- Bot shell TCP socket ---

    def _connect_bot_shell(self) -> None:
        """Connect to the ADB-forwarded bot_shell socket."""
        self._close_socket()
        self._ready = False

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.connect(("127.0.0.1", self.config.bot_shell_local_port))
            sock.settimeout(None)
            self._sock = sock
            logger.info("Connected to bot_shell TCP")

            # Start receive thread
            self._recv_thread = threading.Thread(
                target=self._recv_loop, daemon=True, name="ohmni-botshell-recv"
            )
            self._recv_thread.start()

        except (OSError, ConnectionRefusedError) as e:
            logger.warning("bot_shell connect failed: %s", e)
            self._sock = None

    def _recv_loop(self) -> None:
        """Receive data from bot_shell socket."""
        sock = self._sock
        if not sock:
            return

        try:
            while self._running and sock is self._sock:
                data = sock.recv(4096)
                if not data:
                    break

                text = data.decode("utf-8", errors="replace")

                # Detect welcome banner
                if not self._ready:
                    if "bot_shell" in text:
                        self._ready = True
                        logger.info("bot_shell ready")
                        # Wake head servo so neck_angle works
                        try:
                            sock.sendall(b"wake_head\n")
                        except OSError:
                            pass
                        continue

                # Accumulate response
                self._response_data += text
                # Signal after a short delay to collect multi-line responses
                self._response_event.set()

        except OSError:
            pass
        finally:
            if sock is self._sock:
                logger.info("bot_shell socket closed, will reconnect")
                self._close_socket()
                if self._running:
                    time.sleep(3)
                    self._connect_bot_shell()

    def _close_socket(self) -> None:
        self._ready = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # --- Camera detection ---

    def detect_cameras(self) -> dict[int, CameraDevice]:
        """Detect cameras by V4L2 card name (not device number).

        Port of server.js lines 273-310. Device numbers change after
        USB re-enumeration, so we match by card name.
        """
        rc, out = self._adb_cmd(
            [
                "shell",
                'for d in /dev/video*; do echo "DEV:$d"; '
                "v4l2-ctl --device=$d --all 2>/dev/null | grep 'Card type'; done",
            ],
            timeout=8,
        )
        if rc != 0:
            logger.warning("Camera detection failed (ADB not ready?) — will retry")
            return self.cameras

        self.cameras = {}
        current_dev = None
        for line in out.split("\n"):
            stripped = line.strip()
            if stripped.startswith("DEV:"):
                current_dev = stripped[4:].strip()
            elif "Card type" in stripped and current_dev:
                card_name = stripped.split(":", 1)[-1].strip()
                for cam_id, spec in CAMERA_SPECS.items():
                    if spec["match"] in card_name:
                        self.cameras[cam_id] = CameraDevice(
                            current_dev, spec["res"], spec["name"]
                        )
                        logger.info(
                            "Camera %d (%s): %s [%s]",
                            cam_id, spec["name"], current_dev, card_name,
                        )

        if not self.cameras:
            logger.warning("No cameras detected — will retry on next capture cycle")

        return self.cameras

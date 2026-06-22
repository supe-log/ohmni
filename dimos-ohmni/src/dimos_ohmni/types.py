"""Ohmni-specific types and configuration."""

from dataclasses import dataclass, field


@dataclass
class OhmniConfig:
    """Configuration for the Ohmni robot connection."""

    ip: str = "192.168.1.194"
    adb_port: int = 5555
    bot_shell_local_port: int = 9999
    bot_shell_sock_path: str = (
        "/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock"
    )
    adb_reconnect_interval: float = 10.0
    camera_resolution: tuple[int, int] = (320, 240)
    lidar_serial_port: str = "/dev/ttyUSB0"
    lidar_min_distance: int = 150  # mm
    lidar_max_distance: int = 8000  # mm
    lidar_body_dist_min: int = 177  # mm — robot body signature filter
    lidar_body_dist_max: int = 190  # mm


@dataclass
class OhmniBatteryStatus:
    """Battery telemetry from the Ohmni."""

    level: float = 0.0  # 0-100%
    charging: bool = False
    raw: str = ""


@dataclass
class OhmniHeadState:
    """Head/neck servo state."""

    angle: int = 512  # 400=down, 512=center, 600=up
    awake: bool = False

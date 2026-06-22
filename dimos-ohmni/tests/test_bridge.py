"""Tests for the ADB + bot_shell bridge."""

from dimos_ohmni.bridge import OhmniBridge, CameraDevice, CAMERA_SPECS
from dimos_ohmni.types import OhmniConfig


def test_config_defaults():
    config = OhmniConfig()
    assert config.ip == "192.168.1.194"
    assert config.adb_port == 5555
    assert config.bot_shell_local_port == 9999


def test_camera_specs():
    assert 0 in CAMERA_SPECS
    assert 1 in CAMERA_SPECS
    assert CAMERA_SPECS[0]["match"] == "See3CAM"
    assert CAMERA_SPECS[1]["match"] == "HD USB Camera"


def test_bridge_init():
    config = OhmniConfig(ip="10.0.0.99")
    bridge = OhmniBridge(config)
    assert bridge._adb_addr == "10.0.0.99:5555"
    assert not bridge.is_ready
    assert bridge.cameras == {}

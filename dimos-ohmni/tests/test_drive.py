"""Tests for the drive train adapter."""

from unittest.mock import MagicMock, call
import math

from dimos_ohmni.drive import OhmniDriveTrain


def test_move_twist_conversion():
    bridge = MagicMock()
    drive = OhmniDriveTrain(bridge)

    # 0.5 m/s forward, 1.0 rad/s left turn
    drive.move_twist(0.5, 1.0)
    bridge.send_command.assert_called_once()
    cmd = bridge.send_command.call_args[0][0]
    # 0.5 m/s * 1000 = 500 mm/s
    # 1.0 rad/s * 180/pi ≈ 57 deg/s
    assert "manual_move 500 57" == cmd


def test_stop():
    bridge = MagicMock()
    drive = OhmniDriveTrain(bridge)
    drive.stop()
    bridge.send_command.assert_called_with("manual_move 0 0")


def test_drive_distance():
    bridge = MagicMock()
    drive = OhmniDriveTrain(bridge)
    drive.drive_distance(1000, 8)
    bridge.send_command.assert_called_with("pre_drive 1000 8")


def test_rotate():
    bridge = MagicMock()
    drive = OhmniDriveTrain(bridge)
    drive.rotate(90, 10)
    bridge.send_command.assert_called_with("pre_rot 90 10")

"""Tests for the RPLidar express-scan decoder."""

import math
import numpy as np
from dimos_ohmni.lidar import OhmniLidarReader
from dimos_ohmni.types import OhmniConfig


def test_lidar_init():
    reader = OhmniLidarReader("192.168.1.194:5555")
    assert reader.scan_count == 0
    assert reader.last_scan == []


def test_body_filter():
    reader = OhmniLidarReader("192.168.1.194:5555")
    # Readings in the body band should be filtered
    assert reader._is_body(182) is True
    assert reader._is_body(177) is True
    assert reader._is_body(190) is True
    # Readings outside should pass
    assert reader._is_body(150) is False
    assert reader._is_body(200) is False


def test_points_xy_empty():
    reader = OhmniLidarReader("192.168.1.194:5555")
    points = reader.get_points_xy()
    assert points.shape == (0, 2)


def test_points_xy_conversion():
    reader = OhmniLidarReader("192.168.1.194:5555")
    # Manually set a scan with known values
    reader.last_scan = [
        {"angle": 0.0, "distance": 1000},    # 1m forward -> (1, 0)
        {"angle": 90.0, "distance": 2000},   # 2m left -> (0, 2)
        {"angle": 180.0, "distance": 500},   # 0.5m back -> (-0.5, 0)
    ]
    points = reader.get_points_xy()
    assert points.shape == (3, 2)
    # 0° forward: x≈1.0, y≈0.0
    assert abs(points[0, 0] - 1.0) < 0.01
    assert abs(points[0, 1]) < 0.01
    # 90° left: x≈0.0, y≈2.0
    assert abs(points[1, 0]) < 0.01
    assert abs(points[1, 1] - 2.0) < 0.01

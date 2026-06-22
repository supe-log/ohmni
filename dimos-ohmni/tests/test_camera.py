"""Tests for camera capture."""

from dimos_ohmni.camera import _extract_jpeg


def test_extract_jpeg_valid():
    # Minimal JPEG: SOI + some bytes + EOI
    data = b"\x00\x00\xff\xd8\x01\x02\x03\xff\xd9\x00\x00"
    result = _extract_jpeg(data)
    assert result == b"\xff\xd8\x01\x02\x03\xff\xd9"


def test_extract_jpeg_no_soi():
    data = b"\x00\x00\x01\x02\xff\xd9"
    result = _extract_jpeg(data)
    assert result is None


def test_extract_jpeg_no_eoi():
    data = b"\xff\xd8\x01\x02\x03"
    result = _extract_jpeg(data)
    assert result is None


def test_extract_jpeg_empty():
    result = _extract_jpeg(b"")
    assert result is None

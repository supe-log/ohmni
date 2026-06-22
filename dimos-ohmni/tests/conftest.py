"""Test fixtures for dimos-ohmni."""

import pytest


@pytest.fixture
def ohmni_config():
    """Default Ohmni config for testing."""
    from dimos_ohmni.types import OhmniConfig
    return OhmniConfig()

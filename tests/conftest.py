"""Shared fixtures. Run with: pip install pytest-homeassistant-custom-component && pytest"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Start the recorder first (it must exist before `hass`), then let HA load custom_components/."""
    yield

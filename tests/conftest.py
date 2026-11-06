"""Shared fixtures. Run with: pip install pytest-homeassistant-custom-component && pytest"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_billing_requests():
    """Keep tests off the network: billing periods are empty unless a test sets them."""
    with patch(
        "custom_components.mast_water.api.MastClient.async_get_billing_periods",
        return_value=[],
    ):
        yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Start the recorder first (it must exist before `hass`), then let HA load custom_components/."""
    yield

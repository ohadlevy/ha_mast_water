from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.mast_water.api import MastApiError, MastCompanyError
from custom_components.mast_water.config_flow import _validate
from custom_components.mast_water.const import (
    API_BASE,
    CARDS_URL,
    CONF_BILLING_ONLY,
    CONF_CARD,
    CONF_COMPANY_ID,
    CONF_USER_TOKEN,
    DOMAIN,
    RESUME_URL,
    VALIDATE_URL,
)
from custom_components.mast_water.helpers import (
    parse_billing_periods,
    parse_cards,
    parse_readings,
)

from .test_billing import SAMPLE
from .test_integration import STATS, fake_get_readings

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "onboarding"


def load_fixture(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text())


def mk_jwt(hours: float = 48) -> str:
    def b(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    exp = int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())
    return f"{b({'alg': 'HS256'})}.{b({'exp': exp})}.sig"


async def run_flow(hass: HomeAssistant, token: str = "token") -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USER_TOKEN: token},
    )


GET_BILLING = "custom_components.mast_water.api.MastClient.async_get_billing_periods"
GET_READINGS = "custom_components.mast_water.api.MastClient.async_get_readings"
PAYER_CARDS = API_BASE + "PersonalAreaAsset/GetPayerWaterCards"
BILLING_ONLY_CARD = "18017458"


def mock_company_12(aioclient_mock) -> None:
    """A billing-only corporation: WaterMeter/* says "Company Not valid", payer cards work."""
    aioclient_mock.get(
        RESUME_URL,
        json=load_fixture("resume_company_12.json"),
        headers={"new-token": mk_jwt()},
    )
    aioclient_mock.get(VALIDATE_URL, json=load_fixture("validate_success.json"))
    aioclient_mock.get(
        CARDS_URL, status=401, json=load_fixture("company_not_valid_401.json")
    )
    aioclient_mock.get(PAYER_CARDS, json=load_fixture("payer_water_cards.json"))


async def test_billing_only_corporation_is_added_with_billing_sensors_only(
    hass: HomeAssistant, aioclient_mock, recorder_mock, hass_storage
) -> None:
    """Regression: billing-only corporations (e.g. company 12) used to be rejected.

    They have no daily readings, so they must be validated and polled through the
    billing endpoint alone and must not get the daily sensors.
    """
    mock_company_12(aioclient_mock)
    with (
        patch(GET_BILLING, return_value=parse_billing_periods(SAMPLE)),
        patch(
            GET_READINGS,
            side_effect=AssertionError("billing-only must not request daily readings"),
        ),
    ):
        result = await run_flow(hass)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BILLING_ONLY] is True
    assert result["data"][CONF_CARD] == BILLING_ONLY_CARD
    assert result["data"][CONF_COMPANY_ID] == "12"
    assert result["result"].state is ConfigEntryState.LOADED

    reg = er.async_get(hass)
    for key in ("meter_reading", "daily_usage"):
        assert (
            reg.async_get_entity_id("sensor", DOMAIN, f"{BILLING_ONLY_CARD}_{key}")
            is None
        )
    end = reg.async_get_entity_id(
        "sensor", DOMAIN, f"{BILLING_ONLY_CARD}_billing_period_end"
    )
    assert hass.states.get(end).state == "2026-10-18"


async def test_billing_only_card_error_is_shown_on_the_card_step(
    hass: HomeAssistant, aioclient_mock
) -> None:
    mock_company_12(aioclient_mock)
    with patch(GET_BILLING, side_effect=MastApiError("HTTP 500", 500)):
        result = await run_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "card"
    assert result["errors"] == {"base": "invalid_card"}


async def test_company_rejected_by_both_card_endpoints_aborts(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """If neither the WaterMeter nor the payer cards accept the company, there is nothing to add."""
    mock_company_12(aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        RESUME_URL,
        json=load_fixture("resume_company_12.json"),
        headers={"new-token": mk_jwt()},
    )
    aioclient_mock.get(VALIDATE_URL, json=load_fixture("validate_success.json"))
    for url in (CARDS_URL, PAYER_CARDS):
        aioclient_mock.get(
            url, status=401, json=load_fixture("company_not_valid_401.json")
        )
    result = await run_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_water_cards"


async def test_daily_corporation_is_not_marked_billing_only(
    hass: HomeAssistant, aioclient_mock, recorder_mock, hass_storage
) -> None:
    aioclient_mock.get(
        RESUME_URL,
        json=load_fixture("resume_success.json"),
        headers={"new-token": mk_jwt()},
    )
    aioclient_mock.get(VALIDATE_URL, json=load_fixture("validate_success.json"))
    aioclient_mock.get(CARDS_URL, json=[{"waterCardNumber": "12345", "address": "x"}])
    with patch(GET_READINGS, fake_get_readings), patch(STATS):
        result = await run_flow(hass)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_BILLING_ONLY not in result["data"]
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, "12345_meter_reading") is not None


async def test_validate_billing_only_uses_billing_not_readings(
    hass: HomeAssistant,
) -> None:
    """Reauth re-validates with the stored flag, so it must not need daily readings either."""
    no_readings = AssertionError("billing-only must not request daily readings")
    with patch(GET_READINGS, side_effect=no_readings):
        with patch(GET_BILLING, return_value=[]):
            # No bill yet is fine: a new account may not have been billed.
            assert (
                await _validate(hass, BILLING_ONLY_CARD, mk_jwt(), "12", True) is None
            )
        with patch(GET_BILLING, side_effect=MastCompanyError("HTTP 401")):
            assert (
                await _validate(hass, BILLING_ONLY_CARD, mk_jwt(), "12", True)
                == "invalid_company"
            )


async def test_multi_company_select_step(hass: HomeAssistant, aioclient_mock) -> None:
    """When multiple companies are present and none active, show company select step."""
    multi_company_data = {
        "isSuccess": True,
        "user": {
            "token": "rotated-credential",
            "userCompanies": [
                {"companyId": 12, "isActiveCompany": False, "companyName": "Mei Sheva"},
                {
                    "companyId": 110,
                    "isActiveCompany": False,
                    "companyName": "Mei Raanana",
                },
            ],
        },
    }
    aioclient_mock.get(
        RESUME_URL,
        json=multi_company_data,
        headers={"new-token": mk_jwt()},
    )
    result = await run_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "company"


async def test_parse_helpers_raise_value_error() -> None:
    with pytest.raises(ValueError, match="expected a list"):
        parse_cards("not-a-list")
    with pytest.raises(ValueError, match="expected a list"):
        parse_readings("not-a-list")

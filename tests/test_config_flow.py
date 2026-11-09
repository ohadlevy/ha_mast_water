from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pathlib import Path

from custom_components.mast_water.const import (
    API_BASE,
    CARDS_URL,
    CONF_USER_TOKEN,
    DOMAIN,
    RESUME_URL,
    VALIDATE_URL,
)
from custom_components.mast_water.helpers import parse_cards, parse_readings

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


async def test_billing_only_corporation_aborts_with_no_daily_readings(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Billing-only corporations (e.g. company 12) have no daily readings and abort with no_daily_readings."""
    aioclient_mock.get(
        RESUME_URL,
        json=load_fixture("resume_company_12.json"),
        headers={"new-token": mk_jwt()},
    )
    aioclient_mock.get(VALIDATE_URL, json=load_fixture("validate_success.json"))
    aioclient_mock.get(
        CARDS_URL, status=401, json=load_fixture("company_not_valid_401.json")
    )
    aioclient_mock.get(
        API_BASE + "PersonalAreaAsset/GetPayerWaterCards",
        json=load_fixture("payer_water_cards.json"),
    )
    result = await run_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_daily_readings"


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

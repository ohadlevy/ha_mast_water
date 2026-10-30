from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.mast_water.const import (
    API_BASE,
    CARDS_URL,
    CONF_COMPANY_ID,
    CONF_USER_TOKEN,
    DOMAIN,
    RESUME_URL,
    VALIDATE_URL,
)


def mk_jwt(hours: float = 48) -> str:
    def b(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    exp = int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())
    return f"{b({'alg': 'HS256'})}.{b({'exp': exp})}.sig"


async def run_flow(
    hass: HomeAssistant, company: str = "110", token: str = "token"
) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USER_TOKEN: token, CONF_COMPANY_ID: company},
    )


async def test_wrong_company_id_is_reported_as_invalid_company(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Resume and asset validation succeed, then MAST answers 401 "Company Not valid".

    That means the X-CompanyId is wrong for this account, not that the session
    token was rejected, so the form must say so.
    """
    aioclient_mock.get(
        RESUME_URL,
        json={"isSuccess": True, "user": {"token": "opaque-user-token"}},
        headers={"new-token": mk_jwt()},
    )
    aioclient_mock.get(VALIDATE_URL, json={"isValid": True})
    aioclient_mock.get(CARDS_URL, status=401, json={"reason": "Company Not valid"})
    aioclient_mock.get(
        API_BASE + "PersonalAreaAsset/GetPayerWaterCards",
        status=401,
        json={"reason": "Company Not valid"},
    )
    result = await run_flow(hass, company="12")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_company"}
    # The company the user typed is what MAST was asked about.
    assert aioclient_mock.mock_calls[-1][3]["X-CompanyId"] == "12"

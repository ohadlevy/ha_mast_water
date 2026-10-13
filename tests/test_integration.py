"""End-to-end tests inside a real Home Assistant, with the MAST API mocked."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import webhook
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mast_water.api import MastApiError, MastAuthError
from custom_components.mast_water.const import (
    CONF_CARD,
    CONF_COMPANY_ID,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.mast_water.helpers import Card, Reading

CARD = "12345"
WEBHOOK_ID = "test_webhook_id_0123456789abcdef"
API = "custom_components.mast_water.api.MastClient"
STATS = "custom_components.mast_water.coordinator.async_add_external_statistics"


def mk_token(hours: float) -> str:
    def b(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    exp = int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp())
    return f"{b({'alg': 'HS256'})}.{b({'exp': exp})}.sig"


def readings() -> list[Reading]:
    """Twelve consecutive days ending yesterday, cumulative, 11.864 m3 apart end to end."""
    values = [380.723, 381.59, 382.531, 384.059, 384.671, 385.86,
              386.898, 387.76, 389.269, 390.051, 391.371, 392.587]
    last = date.today() - timedelta(days=1)
    return [Reading(last - timedelta(days=11 - i), v) for i, v in enumerate(values)]


async def fake_get_readings(self, date_from, date_to):
    return [r for r in readings() if date_from <= r.day <= date_to]


def make_entry(token: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=CARD,
        data={
            CONF_CARD: CARD,
            CONF_TOKEN: token,
            CONF_COMPANY_ID: "110",
            CONF_WEBHOOK_ID: WEBHOOK_ID,
        },
    )


@pytest.fixture
def hook_path() -> str:
    return webhook.async_generate_path(WEBHOOK_ID)


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ----------------------------------------------------------------------- config flow


async def test_config_flow_happy_path(hass: HomeAssistant, recorder_mock, hass_storage):
    token = mk_token(48)
    with (
        patch(f"{API}.async_get_cards", AsyncMock(return_value=[Card(CARD, "Test St")])),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TOKEN: f"Bearer {token}", CONF_COMPANY_ID: "110"},
        )
        # One card on the account: the card step is skipped.
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "webhook"
        assert "internal_url" in result["description_placeholders"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        data = result["data"]
        assert data[CONF_CARD] == CARD
        assert data[CONF_TOKEN] == token  # "Bearer " prefix stripped
        assert len(data[CONF_WEBHOOK_ID]) >= 16
        await hass.async_block_till_done()


async def test_config_flow_multiple_cards_shows_picker(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    cards = [Card("111", "A"), Card("222", "B")]
    with (
        patch(f"{API}.async_get_cards", AsyncMock(return_value=cards)),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: mk_token(48), CONF_COMPANY_ID: "110"}
        )
        assert result["step_id"] == "card"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CARD: "222"}
        )
        assert result["step_id"] == "webhook"


@pytest.mark.parametrize(
    ("token", "error"),
    [("not a jwt", "invalid_token"), (mk_token(-1), "token_expired")],
)
async def test_config_flow_bad_tokens(hass: HomeAssistant, token, error):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: token, CONF_COMPANY_ID: "110"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_config_flow_rejected_token(hass: HomeAssistant):
    with patch(f"{API}.async_get_cards", AsyncMock(side_effect=MastAuthError("401"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: mk_token(48), CONF_COMPANY_ID: "110"}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_card_list_fails_falls_back_to_typing(hass: HomeAssistant):
    with patch(f"{API}.async_get_cards", AsyncMock(side_effect=MastApiError("HTTP 500"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: mk_token(48), CONF_COMPANY_ID: "110"}
        )
    assert result["step_id"] == "card"  # free-text card number


# ---------------------------------------------------------------------------- setup


async def test_setup_imports_statistics_and_creates_sensors(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS) as stats:
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    stats.assert_called_once()
    _, metadata, rows = stats.call_args.args
    assert metadata["statistic_id"] == f"{DOMAIN}:meter_{CARD}"
    assert metadata["source"] == DOMAIN
    assert metadata["has_sum"] is True
    assert len(rows) == 12
    assert rows[0]["sum"] == 0.0
    assert rows[-1]["sum"] == pytest.approx(392.587 - 380.723)
    assert all(r["start"].hour == 0 and r["start"].tzinfo for r in rows)

    reg = er.async_get(hass)
    reading = hass.states.get(reg.async_get_entity_id("sensor", DOMAIN, f"{CARD}_meter_reading"))
    usage = hass.states.get(reg.async_get_entity_id("sensor", DOMAIN, f"{CARD}_daily_usage"))
    expiry = hass.states.get(reg.async_get_entity_id("sensor", DOMAIN, f"{CARD}_token_expiry"))
    assert float(reading.state) == pytest.approx(392.587)
    assert float(usage.state) == pytest.approx(1.216)
    assert expiry.state not in ("unknown", "unavailable")


async def test_second_run_imports_only_new_days(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS) as stats:
        await setup_entry(hass, entry)
        assert stats.call_count == 1
        # A refresh with no new day must not import (or re-import) anything.
        await entry.runtime_data.async_refresh()
        assert stats.call_count == 1


async def test_expired_token_at_startup_starts_reauth(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(-1))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


# ------------------------------------------------------------------------- webhook


async def webhook_client(hass, hass_client_no_auth):
    """The HTTP server must exist before the test client can be created."""
    assert await async_setup_component(hass, "webhook", {})
    return await hass_client_no_auth()


async def post(client, path, body):
    resp = await client.post(path, json=body)
    return resp.status, await resp.json()


async def test_webhook_stores_newer_token_and_reloads(
    hass: HomeAssistant, hass_client_no_auth, hook_path, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(10))
    client = await webhook_client(hass, hass_client_no_auth)
    newer = mk_token(48)
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
        status, body = await post(client, hook_path, {"token": f"Bearer {newer}"})
        await hass.async_block_till_done()
    assert status == 200 and body["changed"] is True
    assert entry.data[CONF_TOKEN] == newer
    assert entry.state is ConfigEntryState.LOADED


async def test_webhook_ignores_same_or_older_token(
    hass: HomeAssistant, hass_client_no_auth, hook_path, recorder_mock, hass_storage
):
    current = mk_token(48)
    entry = make_entry(current)
    client = await webhook_client(hass, hass_client_no_auth)
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
        same = await post(client, hook_path, {"token": current})
        older = await post(client, hook_path, {"token": mk_token(5)})
    assert same == (200, {"ok": True, "changed": False})
    assert older == (200, {"ok": True, "changed": False})
    assert entry.data[CONF_TOKEN] == current


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({"nope": 1}, 400, "bad_request"),
        ({"token": "garbage"}, 400, "invalid_or_expired_token"),
        ({"token": mk_token(-1)}, 400, "invalid_or_expired_token"),
    ],
)
async def test_webhook_rejects_bad_input(
    hass: HomeAssistant, hass_client_no_auth, hook_path, recorder_mock, hass_storage,
    body, status, error,
):
    entry = make_entry(mk_token(10))
    client = await webhook_client(hass, hass_client_no_auth)
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
        got_status, got = await post(client, hook_path, body)
    assert got_status == status and got["error"] == error


async def test_webhook_refuses_token_that_mast_rejects(
    hass: HomeAssistant, hass_client_no_auth, hook_path, recorder_mock, hass_storage
):
    original = mk_token(10)
    entry = make_entry(original)
    client = await webhook_client(hass, hass_client_no_auth)
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
    # Token from some other account: MAST says no -> must not replace the good one.
    with patch(f"{API}.async_get_readings", AsyncMock(side_effect=MastApiError("HTTP 500"))):
        status, body = await post(client, hook_path, {"token": mk_token(48)})
    assert status == 400 and body["error"] == "token_not_valid_for_this_card"
    with patch(f"{API}.async_get_readings", AsyncMock(side_effect=MastAuthError("401"))):
        status, body = await post(client, hook_path, {"token": mk_token(49)})
    assert status == 401
    assert entry.data[CONF_TOKEN] == original


async def test_webhook_recovers_an_entry_that_failed_on_expired_token(
    hass: HomeAssistant, hass_client_no_auth, hook_path, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(-1))
    client = await webhook_client(hass, hass_client_no_auth)
    fresh = mk_token(48)
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)

        status, body = await post(client, hook_path, {"token": fresh})
        await hass.async_block_till_done()

    assert status == 200 and body["changed"] is True
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TOKEN] == fresh
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN), "reauth prompt must be gone"


async def test_unload_removes_webhook(
    hass: HomeAssistant, hass_client_no_auth, hook_path, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    client = await webhook_client(hass, hass_client_no_auth)
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        resp = await client.post(hook_path, json={"token": mk_token(48)})
    # HA answers 200 with an empty body for unknown webhook ids (so ids can't be probed).
    assert (await resp.text()) == ""

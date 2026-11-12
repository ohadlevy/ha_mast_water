"""Billing-period data (GetWaterCardConsumptions): parsing and sensors."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mast_water.api import MastApiError, MastAuthError
from custom_components.mast_water.const import (
    CONF_BILLING_ONLY,
    CONF_CARD,
    CONF_COMPANY_ID,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.mast_water.helpers import (
    billing_from_dict,
    billing_to_dict,
    parse_billing_periods,
)

from .test_integration import (
    API,
    CARD,
    STATS,
    fake_get_readings,
    make_entry,
    mk_token,
    setup_entry,
)

# Invented numbers in the shape of the real response (field names as MAST sends them).
SAMPLE = [
    {
        "calculationId": 1,
        "year": 2026,
        "period": 4,
        "ypKey": "202604",
        "consumptionDays": 61,
        "lastReading": "2026-08-18T00:00:00",
        "personalUsage": 10.5,
        "legacyUsage": 3.25,
        "totalUsage": 13.75,
        "waterPrice": 90.0,
        "sewagPrice": 40.0,
        "totalPrice": 130.0,
    },
    {
        "calculationId": 2,
        "year": 2026,
        "period": 5,
        "ypKey": "202605",
        "consumptionDays": 43,
        "lastReading": "2026-10-18T00:00:00",
        "personalUsage": 7.5,
        "legacyUsage": 4.0,
        "totalUsage": 11.5,
        "waterPrice": 70.0,
        "sewagPrice": 30.0,
        "totalPrice": 100.0,
    },
]

GET_BILLING = f"{API}.async_get_billing_periods"


def test_parse_orders_periods_and_maps_fields():
    periods = parse_billing_periods(list(reversed(SAMPLE)))
    assert [p.end for p in periods] == [date(2026, 8, 18), date(2026, 10, 18)]
    last = periods[-1]
    assert last.days == 43
    assert last.private_usage == 7.5
    assert last.shared_usage == 4.0
    assert last.total_usage == 11.5
    assert last.water_price == 70.0
    assert last.sewage_price == 30.0
    assert last.total_price == 100.0
    assert (last.year, last.period) == (2026, 5)


def test_parse_accepts_day_first_dates():
    periods = parse_billing_periods([{**SAMPLE[1], "lastReading": "18/10/2026"}])
    assert periods[0].end == date(2026, 10, 18)


def test_parse_skips_entries_without_a_usable_end_date_and_bad_numbers():
    periods = parse_billing_periods(
        [
            "not a dict",
            {**SAMPLE[0], "lastReading": None},
            {**SAMPLE[0], "lastReading": "garbage"},
            {**SAMPLE[1], "personalUsage": "x", "totalPrice": True},
        ]
    )
    assert len(periods) == 1
    assert periods[0].private_usage is None
    assert periods[0].total_price is None
    assert periods[0].shared_usage == 4.0


def test_billing_period_survives_storage_round_trip():
    for period in parse_billing_periods(SAMPLE):
        assert billing_from_dict(billing_to_dict(period)) == period
    assert billing_from_dict({"end": "garbage"}) is None
    assert billing_from_dict("x") is None


def test_parse_rejects_a_non_list():
    with pytest.raises(TypeError):
        parse_billing_periods({"error": "x"})


def _state(hass: HomeAssistant, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{CARD}_{key}"
    )
    assert entity_id is not None, key
    return hass.states.get(entity_id)


async def test_billing_sensors_show_the_latest_period(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    periods = parse_billing_periods(SAMPLE)
    with (
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(GET_BILLING, return_value=periods),
        patch(STATS),
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    end = _state(hass, "billing_period_end")
    assert end.state == "2026-10-18"
    assert end.attributes["days"] == 43
    assert end.attributes["water_price"] == 70.0
    history = end.attributes["history"]
    assert [h["end"] for h in history] == ["2026-08-18", "2026-10-18"]
    assert history[0]["private_usage"] == 10.5
    assert history[1]["shared_usage"] == 4.0
    # The periods are saved, so they survive MAST no longer returning them.
    saved = hass_storage[f"{DOMAIN}.{CARD}.billing"]["data"]["periods"]
    assert [p["end"] for p in saved] == ["2026-08-18", "2026-10-18"]
    assert float(_state(hass, "billing_private_usage").state) == pytest.approx(7.5)
    assert float(_state(hass, "billing_shared_usage").state) == pytest.approx(4.0)
    assert float(_state(hass, "billing_total_price").state) == pytest.approx(100.0)


@pytest.mark.parametrize("error", [MastApiError("HTTP 500", 500), MastAuthError("x")])
async def test_billing_failure_never_breaks_the_meter_update(
    hass: HomeAssistant, recorder_mock, hass_storage, error
):
    entry = make_entry(mk_token(48))
    with (
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(GET_BILLING, side_effect=error),
        patch(STATS),
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert float(_state(hass, "meter_reading").state) == pytest.approx(392.587)
    assert _state(hass, "billing_period_end").state == "unavailable"


async def test_known_period_survives_a_later_billing_failure(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    periods = parse_billing_periods(SAMPLE)
    with (
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(GET_BILLING, return_value=periods),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
    with (
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(GET_BILLING, side_effect=MastApiError("HTTP 500", 500)),
        patch(STATS),
    ):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert _state(hass, "billing_period_end").state == "2026-10-18"


# ------------------------------------------------------------- billing-only entries


def make_billing_only_entry(token: str) -> MockConfigEntry:
    """A corporation with no daily readings, as saved by the config flow."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=CARD,
        data={
            CONF_CARD: CARD,
            CONF_TOKEN: token,
            CONF_COMPANY_ID: "12",
            CONF_BILLING_ONLY: True,
        },
    )


NO_READINGS = AssertionError("billing-only must not request daily readings")


async def test_billing_only_entry_polls_billing_and_nothing_else(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_billing_only_entry(mk_token(48))
    with (
        patch(f"{API}.async_get_readings", side_effect=NO_READINGS),
        patch(GET_BILLING, return_value=parse_billing_periods(SAMPLE)),
        patch(STATS) as stats,
    ):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    stats.assert_not_called()  # no readings, so nothing for the Energy dashboard
    assert _state(hass, "billing_period_end").state == "2026-10-18"
    assert float(_state(hass, "billing_private_usage").state) == pytest.approx(7.5)
    assert _state(hass, "token_expiry").state not in ("unknown", "unavailable")
    reg = er.async_get(hass)
    for key in ("meter_reading", "daily_usage"):
        assert reg.async_get_entity_id("sensor", DOMAIN, f"{CARD}_{key}") is None


async def test_billing_only_entry_without_a_bill_yet_still_loads(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_billing_only_entry(mk_token(48))
    with (
        patch(f"{API}.async_get_readings", side_effect=NO_READINGS),
        patch(GET_BILLING, return_value=[]),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, "billing_period_end").state == "unavailable"


async def test_billing_only_errors_are_not_swallowed(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    """For daily corporations billing is best effort; for billing-only it is the data."""
    entry = make_billing_only_entry(mk_token(48))
    with (
        patch(f"{API}.async_get_readings", side_effect=NO_READINGS),
        patch(GET_BILLING, side_effect=MastApiError("HTTP 500", 500)),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_billing_only_auth_failure_starts_reauth(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_billing_only_entry(mk_token(48))
    with (
        patch(f"{API}.async_get_readings", side_effect=NO_READINGS),
        patch(GET_BILLING, side_effect=MastAuthError("HTTP 401")),
        patch(f"{API}.async_restore_session", AsyncMock(return_value=False)),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_billing_only_refusal_restores_the_session_and_retries(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    """The session repair that protects daily readings also protects the billing call."""
    entry = make_billing_only_entry(mk_token(48))
    periods = parse_billing_periods(SAMPLE)
    state = {"restored": False}

    async def restore_session(self):
        state["restored"] = True
        return True

    async def refuses_until_restored(self):
        if not state["restored"]:
            raise MastAuthError("HTTP 401, reason: Expired|NotValid")
        return periods

    with (
        patch(f"{API}.async_get_readings", side_effect=NO_READINGS),
        patch(GET_BILLING, refuses_until_restored),
        patch(f"{API}.async_restore_session", restore_session),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, "billing_period_end").state == "2026-10-18"

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

from custom_components.mast_water.api import (
    MastApiError,
    MastAuthError,
    MastClient as _MastClient,
)
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


def limited_get_readings(max_span_days: int):
    """Fake API that rejects ranges longer than the web app allows (HTTP 400)."""

    async def _get(self, date_from, date_to):
        if (date_to - date_from).days > max_span_days:
            raise MastApiError("HTTP 400", 400)
        return [r for r in readings() if date_from <= r.day <= date_to]

    return _get


# The web app allows at most 30 days between dateFrom and dateTo.
fake_get_readings = limited_get_readings(30)


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
    [
        ("", "missing_credentials"),
        ("not a jwt", "invalid_token"),
        (mk_token(-1), "token_expired"),
    ],
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
        ({"token": mk_token(48), "user_token": {"bad": "type"}}, 400, "bad_request"),
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


# ------------------------------------------------------ renewed token (`new-token`)


def test_client_adopts_only_valid_longer_lived_new_token():
    from custom_components.mast_water.api import MastClient

    old, newer, older = mk_token(10), mk_token(48), mk_token(2)
    c = MastClient(None, old, CARD, "110")
    c._adopt_new_token(None)
    c._adopt_new_token("garbage")
    c._adopt_new_token(f"Bearer {older}")  # shorter-lived: ignored
    assert c.token == old
    c._adopt_new_token(f"Bearer {newer}")
    assert c.token == newer


async def test_renewed_token_is_persisted_by_the_coordinator(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    old = mk_token(10)
    renewed = mk_token(48)
    entry = make_entry(old)

    async def readings_and_renew(self, date_from, date_to):
        self._adopt_new_token(f"Bearer {renewed}")  # what the real client does on a response
        return await fake_get_readings(self, date_from, date_to)

    with patch(f"{API}.async_get_readings", readings_and_renew), patch(STATS):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TOKEN] == renewed
    reg = er.async_get(hass)
    expiry = hass.states.get(reg.async_get_entity_id("sensor", DOMAIN, f"{CARD}_token_expiry"))
    assert datetime.fromisoformat(expiry.state) > datetime.now(timezone.utc) + timedelta(hours=40)


# --------------------------------------------------- range limits and "no data"


async def test_requests_never_exceed_the_30_day_limit(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    spans: list[int] = []

    async def spy(self, date_from, date_to):
        spans.append((date_to - date_from).days)
        return await fake_get_readings(self, date_from, date_to)

    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", spy), patch(STATS) as stats:
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert spans and max(spans) <= 29  # same 30-day window the web app uses by default
    assert len(stats.call_args.args[2]) == 12  # full history still imported


async def test_falls_back_to_7_day_ranges_when_mast_rejects_30(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    """Some water corporations (Arad meters) only allow 7 days per request."""
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", limited_get_readings(6)), patch(STATS) as stats:
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(stats.call_args.args[2]) == 12
    assert entry.runtime_data._chunk_days == 7


async def test_404_means_no_data_not_an_error(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    async def old_empty(self, date_from, date_to):
        if date_to < date.today() - timedelta(days=20):
            raise MastApiError("HTTP 404", 404)  # web app: 404 = empty result
        return await fake_get_readings(self, date_from, date_to)

    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", old_empty), patch(STATS) as stats:
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(stats.call_args.args[2]) == 12


async def test_other_errors_on_the_latest_chunk_still_fail_the_update(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", AsyncMock(side_effect=MastApiError("HTTP 500", 500))), patch(STATS):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


# ------------------------------------------------------------------ error messages


async def _call_with_response(status: int, body: str, content_type: str = "application/json"):
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer

    from custom_components.mast_water.api import MastClient

    async def handler(request):
        return web.Response(status=status, text=body, content_type=content_type)

    app = web.Application()
    app.router.add_post("/r", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with ClientSession() as session:
            client = MastClient(session, mk_token(48), CARD, "110", server.make_url("/r"))
            return await client.async_get_readings(date(2026, 10, 1), date(2026, 10, 2))
    finally:
        await server.close()


async def test_401_message_includes_status_and_reason(socket_enabled):
    with pytest.raises(MastAuthError, match=r"HTTP 401, reason: expired"):
        await _call_with_response(401, '{"reason": "expired"}')


async def test_403_json_is_auth_but_403_html_is_a_block_not_expiry(socket_enabled):
    with pytest.raises(MastAuthError, match="HTTP 403"):
        await _call_with_response(403, '{"message": "no"}')
    with pytest.raises(MastApiError, match="blocked") as err:
        await _call_with_response(403, "<html>Just a moment...</html>", "text/html")
    assert err.value.status == 403 and not isinstance(err.value, MastAuthError)


async def test_blocked_403_does_not_start_reauth(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", AsyncMock(side_effect=MastApiError("HTTP 403 (blocked)", 403))), patch(STATS):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY  # retried later, no login prompt
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_token_sensor_exposes_issue_time_and_renewals(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
    reg = er.async_get(hass)
    state = hass.states.get(reg.async_get_entity_id("sensor", DOMAIN, f"{CARD}_token_expiry"))
    assert state.attributes["renewals_since_start"] == 0
    assert state.attributes["issued"] is None  # test tokens carry no iat claim


# ------------------------------------------ refused-but-not-expired tokens are retried


async def test_refused_live_token_is_retried_before_asking_for_login(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    reg = er.async_get(hass)
    token_sensor = reg.async_get_entity_id("sensor", DOMAIN, f"{CARD}_token_expiry")

    refused = AsyncMock(side_effect=MastAuthError("HTTP 401, reason: expired"))
    with patch(f"{API}.async_get_readings", refused), patch(STATS):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.auth_failures == 1
        assert not coordinator.last_update_success
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert entry.state is ConfigEntryState.LOADED
        # The token sensor stays visible and explains what happened.
        state = hass.states.get(token_sensor)
        assert state.state != "unavailable"
        assert state.attributes["rejections_in_a_row"] == 1
        assert "HTTP 401" in state.attributes["last_rejection"]

        await coordinator.async_refresh()
        assert coordinator.auth_failures == 2
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

        await coordinator.async_refresh()  # third strike
        await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)] == [SOURCE_REAUTH]


async def test_success_after_a_refusal_resets_the_counter(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
        coordinator = entry.runtime_data
        with patch(f"{API}.async_get_readings", AsyncMock(side_effect=MastAuthError("HTTP 401"))):
            await coordinator.async_refresh()
        assert coordinator.auth_failures == 1
        await coordinator.async_refresh()
    assert coordinator.auth_failures == 0
    assert coordinator.last_update_success
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_refused_token_that_has_expired_asks_for_login_immediately(
    hass: HomeAssistant, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
        coordinator = entry.runtime_data
        coordinator._expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
        # The proactive expiry check fires before any request is made.
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)] == [SOURCE_REAUTH]


# --------------------------------------------- restoring the MAST session on a refusal


_REAL_RESTORE = _MastClient.async_restore_session  # captured before the fixture below replaces it


@pytest.fixture(autouse=True)
def restore():
    """The session-restoring calls are stubbed so that no coordinator test touches the network."""
    with patch(f"{API}.async_restore_session", AsyncMock(return_value=True)) as mock:
        yield mock


async def test_refusal_of_a_valid_token_restores_the_session_and_retries(
    hass: HomeAssistant, recorder_mock, hass_storage, restore
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    state = {"restored": False}

    async def restore_session():
        state["restored"] = True
        return True

    async def refuses_until_restored(self, date_from, date_to):
        if not state["restored"]:
            raise MastAuthError("HTTP 401, reason: Expired|NotValid")
        return await fake_get_readings(self, date_from, date_to)

    restore.side_effect = restore_session
    with patch(f"{API}.async_get_readings", refuses_until_restored), patch(STATS):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.auth_failures == 0
    restore.assert_awaited_once()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_refusal_that_restoring_does_not_cure_counts_one_strike(
    hass: HomeAssistant, recorder_mock, hass_storage, restore
):
    entry = make_entry(mk_token(48))
    with patch(f"{API}.async_get_readings", fake_get_readings), patch(STATS):
        await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    refused = AsyncMock(side_effect=MastAuthError("HTTP 401, reason: Expired|NotValid"))
    with patch(f"{API}.async_get_readings", refused), patch(STATS):
        await coordinator.async_refresh()
    assert refused.await_count == 2  # the request and one retry after restoring
    restore.assert_awaited_once()
    assert coordinator.auth_failures == 1


async def test_expired_token_is_neither_restored_nor_retried(
    hass: HomeAssistant, recorder_mock, hass_storage, restore
):
    entry = make_entry(mk_token(-1))
    refused = AsyncMock(side_effect=MastAuthError("HTTP 401, reason: expired"))
    with patch(f"{API}.async_get_readings", refused), patch(STATS):
        await setup_entry(hass, entry)
    assert refused.await_count == 0  # caught before any request
    restore.assert_not_awaited()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)] == [SOURCE_REAUTH]


async def _restore_with_server(handlers: dict):
    """Run async_restore_session against a local server; return (result, request paths in order)."""
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer

    from custom_components.mast_water.api import MastClient

    seen: list[str] = []

    def wrap(path):
        async def handler(request):
            seen.append(path)
            return handlers[path](request)

        return handler

    app = web.Application()
    for path in handlers:
        app.router.add_get(path, wrap(path))
    server = TestServer(app)
    await server.start_server()
    try:
        async with ClientSession() as session:
            client = MastClient(
                session, mk_token(48), CARD, "110",
                cards_url=str(server.make_url("/cards")),
                validate_url=str(server.make_url("/validate")),
            )
            return await _REAL_RESTORE(client), seen
    finally:
        await server.close()


async def test_restore_session_validates_assets_then_loads_cards(socket_enabled):
    from aiohttp import web

    ok, seen = await _restore_with_server(
        {
            "/validate": lambda r: web.json_response({"isValid": True, "message": None}),
            "/cards": lambda r: web.json_response([{"waterCardNumber": CARD, "address": "x"}]),
        }
    )
    assert ok is True
    assert seen == ["/validate", "/cards"]  # in the web app's order


async def test_restore_session_reports_failure(socket_enabled):
    from aiohttp import web

    ok, seen = await _restore_with_server(
        {
            "/validate": lambda r: web.json_response({"isValid": False, "message": "no"}),
            "/cards": lambda r: web.json_response([]),
        }
    )
    assert ok is False
    ok, seen = await _restore_with_server(
        {
            "/validate": lambda r: web.json_response({"reason": "expired"}, status=401),
            "/cards": lambda r: web.json_response([]),
        }
    )
    assert ok is False and seen == ["/validate"]  # stops at the first failure

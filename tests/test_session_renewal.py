"""Session renewal, failure handling, and credential binding inside Home Assistant."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mast_water.api import (
    MastApiError,
    MastAuthError,
    MastClient,
    MastConnectionError,
)
from custom_components.mast_water.const import CONF_TOKEN, CONF_USER_TOKEN, DOMAIN
from .test_integration import (
    API,
    CARD,
    STATS,
    fake_get_readings,
    make_entry,
    mk_token,
    setup_entry,
)


def renewable_entry(token):
    original = make_entry(token)
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=CARD,
        data={**original.data, CONF_USER_TOKEN: "remembered-credential"},
    )


def renew_with(token, calls=None):
    async def renew(self):
        if calls is not None:
            calls.append(self.token)
        self._adopt_new_token(token)
        self._user_token = "rotated-credential"

    return renew


@pytest.mark.parametrize("old_token", ["", "expired"])
async def test_resume_exchanges_opaque_token_without_old_jwt(socket_enabled, old_token):
    renewed = mk_token(48)

    async def resume(request):
        assert request.query["token"] == "remembered-credential"
        assert "jwt" not in request.headers
        assert "Cookie" not in request.headers
        assert request.headers["X-CompanyId"] == "110"
        return web.json_response(
            {"isSuccess": True, "user": {"token": "rotated-credential"}},
            headers={"new-token": "Bearer " + renewed},
        )

    app = web.Application()
    app.router.add_get("/resume", resume)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = MastClient(
            session,
            mk_token(-1) if old_token else "",
            CARD,
            "110",
            user_token="remembered-credential",
            resume_url=str(server.make_url("/resume")),
        )
        await client.async_resume_session()
        assert client.token == renewed
        assert client.user_token == "rotated-credential"


@pytest.mark.parametrize(
    "mode",
    [
        "rejected",
        "missing_jwt",
        "expired_jwt",
        "server_error",
        "auth_error",
        "redirect",
    ],
)
async def test_resume_failure_is_sanitized_and_preserves_credentials(
    socket_enabled, caplog, mode
):
    old, newer = mk_token(10), mk_token(48)

    async def resume(request):
        body = {
            "isSuccess": mode != "rejected",
            "user": {"token": "untrusted-rotation"},
            "message": "request echoed token=remembered-credential",
        }
        status = {"server_error": 500, "auth_error": 401, "redirect": 302}.get(
            mode, 200
        )
        headers = {"new-token": mk_token(-1) if mode == "expired_jwt" else newer}
        if mode == "missing_jwt":
            headers = {}
        if mode == "redirect":
            headers["Location"] = "/forwarded"
        return web.json_response(body, status=status, headers=headers)

    async def forwarded(request):
        pytest.fail("The credential-bearing request must not follow redirects")

    app = web.Application()
    app.router.add_get("/resume", resume)
    app.router.add_get("/forwarded", forwarded)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = MastClient(
            session,
            old,
            CARD,
            "110",
            user_token="remembered-credential",
            resume_url=str(server.make_url("/resume")),
        )
        with pytest.raises((MastAuthError, MastApiError)) as error:
            await client.async_resume_session()
        assert "remembered-credential" not in str(error.value)
        assert client.token == old
        assert client.user_token == "remembered-credential"
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.mast_water")
    ]
    assert "remembered-credential" not in " ".join(messages)


async def test_resume_connection_exception_hides_query_credential():
    session = MagicMock()
    session.request.side_effect = aiohttp.ClientError("url?token=remembered-credential")
    client = MastClient(
        session, mk_token(10), CARD, "110", user_token="remembered-credential"
    )
    with pytest.raises(MastConnectionError) as error:
        await client.async_resume_session()
    assert "remembered-credential" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("hours", [6, -1])
async def test_coordinator_renews_and_persists_both_credentials(
    hass, recorder_mock, hass_storage, hours
):
    old, renewed = mk_token(hours), mk_token(48)
    entry = renewable_entry(old)
    calls = []
    with (
        patch(f"{API}.async_resume_session", renew_with(renewed, calls)),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.data[CONF_TOKEN] == renewed
        assert entry.data[CONF_USER_TOKEN] == "rotated-credential"
        assert entry.runtime_data.data.renewals == 1
        await entry.runtime_data.async_refresh()
    assert calls == [old]  # A fresh JWT is not renewed on every poll.
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.parametrize(
    "error",
    [
        MastConnectionError("offline"),
        MastApiError("HTTP 500", 500),
        MastAuthError("rejected"),
    ],
)
async def test_failed_early_renewal_keeps_using_valid_jwt(
    hass, recorder_mock, hass_storage, error
):
    old = mk_token(6)
    entry = renewable_entry(old)
    resume = AsyncMock(side_effect=error)
    with (
        patch(f"{API}.async_resume_session", resume),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
        await entry.runtime_data.async_refresh()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TOKEN] == old
    assert resume.await_count == (1 if isinstance(error, MastAuthError) else 2)
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.parametrize(
    "error", [MastConnectionError("offline"), MastApiError("HTTP 500", 500)]
)
async def test_expired_jwt_with_temporary_renewal_failure_retries_without_reauth(
    hass, recorder_mock, hass_storage, error
):
    entry = renewable_entry(mk_token(-1))
    with (
        patch(f"{API}.async_resume_session", AsyncMock(side_effect=error)),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_expired_jwt_with_rejected_credential_starts_reauth(
    hass, recorder_mock, hass_storage
):
    entry = renewable_entry(mk_token(-1))
    with (
        patch(
            f"{API}.async_resume_session",
            AsyncMock(side_effect=MastAuthError("rejected")),
        ),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_rotated_credentials_survive_subsequent_data_failure(
    hass, recorder_mock, hass_storage
):
    entry = renewable_entry(mk_token(6))
    renewed = mk_token(48)
    with (
        patch(f"{API}.async_resume_session", renew_with(renewed)),
        patch(
            f"{API}.async_get_readings",
            AsyncMock(side_effect=MastConnectionError("offline")),
        ),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.data[CONF_TOKEN] == renewed
    assert entry.data[CONF_USER_TOKEN] == "rotated-credential"


async def test_initial_flow_accepts_resume_credential_without_valid_jwt(
    hass, recorder_mock, hass_storage
):
    from custom_components.mast_water.helpers import Card

    renewed = mk_token(48)
    calls = []

    async def resume(self):
        calls.append("resume")
        await renew_with(renewed)(self)

    async def restore(self):
        calls.append("restore")
        return True

    async def cards(self):
        calls.append("cards")
        return [Card(CARD, "Test")]

    with (
        patch(f"{API}.async_resume_session", resume),
        patch(f"{API}.async_restore_session", restore),
        patch(f"{API}.async_get_cards", cards),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        user_input = {CONF_USER_TOKEN: "remembered-credential", "company_id": "110"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_TOKEN] == renewed
        assert result["data"][CONF_USER_TOKEN] == "rotated-credential"
        await hass.async_block_till_done()
    assert calls[:3] == ["resume", "restore", "cards"]


async def test_setup_form_requires_only_session_token(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    fields = {key.schema for key in result["data_schema"].schema}
    assert CONF_USER_TOKEN in fields
    assert CONF_TOKEN not in fields
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USER_TOKEN: "", "company_id": "110"}
    )
    assert result["errors"] == {"base": "session_token_required"}


async def test_setup_keeps_multiple_card_picker(hass):
    from custom_components.mast_water.helpers import Card

    cards = [Card("111", "A"), Card("222", "B")]
    with (
        patch(f"{API}.async_resume_session", renew_with(mk_token(48))),
        patch(f"{API}.async_restore_session", AsyncMock(return_value=True)),
        patch(f"{API}.async_get_cards", AsyncMock(return_value=cards)),
        patch(f"{API}.async_get_readings", fake_get_readings),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: "credential", "company_id": "110"}
        )
        assert result["step_id"] == "card"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"card_number": "222"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["card_number"] == "222"


@pytest.mark.parametrize(
    ("stage", "failure", "expected"),
    [
        ("resume", MastAuthError("rejected"), "invalid_user_token"),
        ("restore", False, "assets_invalid"),
        ("restore", MastConnectionError("offline"), "cannot_connect"),
        ("restore", MastApiError("HTTP 500", 500), "mast_error"),
    ],
)
async def test_setup_reports_resume_and_restore_failures(
    hass, stage, failure, expected
):
    resume = AsyncMock(side_effect=failure if stage == "resume" else None)
    restore = AsyncMock(
        side_effect=failure
        if stage == "restore" and isinstance(failure, Exception)
        else None,
        return_value=failure if failure is False else True,
    )
    with (
        patch(f"{API}.async_resume_session", resume),
        patch(f"{API}.async_restore_session", restore),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: "credential", "company_id": "110"}
        )
    assert result["errors"] == {"base": expected}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (False, "assets_invalid"),
        (MastConnectionError("offline"), "cannot_connect"),
        (MastApiError("HTTP 500", 500), "mast_error"),
    ],
)
async def test_reconfigure_reports_restore_failures(hass, failure, expected):
    entry = make_entry(mk_token(-1))
    entry.add_to_hass(hass)
    restore = AsyncMock(
        side_effect=failure if isinstance(failure, Exception) else None,
        return_value=failure if failure is False else True,
    )
    with (
        patch(f"{API}.async_resume_session", renew_with(mk_token(48))),
        patch(f"{API}.async_restore_session", restore),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: "credential"}
        )
    assert result["errors"] == {"base": expected}
    assert CONF_USER_TOKEN not in entry.data


@pytest.mark.parametrize("card_matches", [True, False])
async def test_reconfigure_validates_resume_credential_for_existing_meter(
    hass, recorder_mock, hass_storage, card_matches
):
    from custom_components.mast_water.helpers import Card

    entry = make_entry(mk_token(-1))
    entry.add_to_hass(hass)
    renewed = mk_token(48)
    calls = []

    async def resume(self):
        calls.append("resume")
        await renew_with(renewed)(self)

    async def restore(self):
        calls.append("restore")
        return True

    async def cards(self):
        calls.append("cards")
        return [Card(CARD if card_matches else "other", "Test")]

    with (
        patch(f"{API}.async_resume_session", resume),
        patch(f"{API}.async_restore_session", restore),
        patch(f"{API}.async_get_cards", cards),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USER_TOKEN: "remembered-credential"}
        )
        if card_matches:
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "reconfigure_successful"
            assert entry.data[CONF_TOKEN] == renewed
            assert entry.data[CONF_USER_TOKEN] == "rotated-credential"
        else:
            assert result["errors"] == {"base": "invalid_user_token"}
            assert CONF_USER_TOKEN not in entry.data
        await hass.async_block_till_done()
    assert calls[:3] == ["resume", "restore", "cards"]


@pytest.mark.parametrize("restore_rejects", [False, True])
async def test_early_server_rejection_renews_before_requesting_otp(
    hass, recorder_mock, hass_storage, restore_rejects
):
    old, renewed = mk_token(48), mk_token(49)
    entry = renewable_entry(old)

    async def readings_require_new_jwt(self, date_from, date_to):
        if self.token == old:
            raise MastAuthError("HTTP 401")
        return await fake_get_readings(self, date_from, date_to)

    restore = AsyncMock(
        side_effect=[MastAuthError("HTTP 401"), True] if restore_rejects else None,
        return_value=True,
    )
    with (
        patch(f"{API}.async_resume_session", renew_with(renewed)),
        patch(f"{API}.async_restore_session", restore),
        patch(f"{API}.async_get_readings", readings_require_new_jwt),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TOKEN] == renewed
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_reauthentication_replaces_rejected_resume_credential(
    hass, recorder_mock, hass_storage
):
    from custom_components.mast_water.helpers import Card

    entry = renewable_entry(mk_token(-1))
    with (
        patch(
            f"{API}.async_resume_session",
            AsyncMock(side_effect=MastAuthError("rejected")),
        ),
        patch(STATS),
    ):
        await setup_entry(hass, entry)
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    assert flow["context"]["source"] == SOURCE_REAUTH
    renewed = mk_token(48)
    with (
        patch(f"{API}.async_resume_session", renew_with(renewed)),
        patch(f"{API}.async_restore_session", AsyncMock(return_value=True)),
        patch(f"{API}.async_get_cards", AsyncMock(return_value=[Card(CARD, "Test")])),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_USER_TOKEN: "new-remembered-credential"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == renewed
    assert entry.data[CONF_USER_TOKEN] == "rotated-credential"
    assert entry.state is ConfigEntryState.LOADED


async def test_legacy_jwt_only_entry_reauth_asks_for_session_token(
    hass, recorder_mock, hass_storage
):
    from custom_components.mast_water.helpers import Card

    entry = make_entry(mk_token(-1))
    with patch(STATS):
        await setup_entry(hass, entry)
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    assert flow["context"]["source"] == SOURCE_REAUTH
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_USER_TOKEN: ""}
    )
    assert result["errors"] == {"base": "session_token_required"}
    fields = {key.schema for key in result["data_schema"].schema}
    assert fields == {CONF_USER_TOKEN}

    renewed = mk_token(48)
    with (
        patch(f"{API}.async_resume_session", renew_with(renewed)),
        patch(f"{API}.async_restore_session", AsyncMock(return_value=True)),
        patch(f"{API}.async_get_cards", AsyncMock(return_value=[Card(CARD, "Test")])),
        patch(f"{API}.async_get_readings", fake_get_readings),
        patch(STATS),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_USER_TOKEN: "credential"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == renewed
    assert entry.data[CONF_USER_TOKEN] == "rotated-credential"
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_reports_invalid_assets_separately(
    hass, recorder_mock, hass_storage
):
    entry = make_entry(mk_token(-1))
    with patch(STATS):
        await setup_entry(hass, entry)
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    with (
        patch(f"{API}.async_resume_session", renew_with(mk_token(48))),
        patch(f"{API}.async_restore_session", AsyncMock(return_value=False)),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_USER_TOKEN: "credential"}
        )
    assert result["errors"] == {"base": "assets_invalid"}

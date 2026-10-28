"""Data coordinator: polls MAST and imports long-term statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MastApiError, MastAuthError, MastClient, MastConnectionError
from .const import (
    AUTH_RETRY_SECONDS,
    AUTH_STRIKES,
    BACKFILL_DAYS,
    CHUNK_DAYS,
    CONF_TOKEN,
    CONF_USER_TOKEN,
    DOMAIN,
    SMALL_CHUNK_DAYS,
    STORE_VERSION,
    TOKEN_RENEWAL_WINDOW,
    UPDATE_INTERVAL,
)
from .helpers import Reading, build_rows, token_expiry, token_issued

_LOGGER = logging.getLogger(__name__)

try:  # Home Assistant 2025.x+ replaced has_mean with mean_type
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover
    StatisticMeanType = None


@dataclass
class MastData:
    """What the sensors show."""

    latest_reading: float | None
    latest_day: date | None
    daily_usage: float | None
    token_expiry: datetime | None
    token_issued: datetime | None = None
    renewals: int = 0


def statistic_id(card: str) -> str:
    """External statistic id used for the Energy dashboard."""
    return f"{DOMAIN}:meter_{card}"


def _metadata(card: str) -> StatisticMetaData:
    meta: dict = {
        "has_sum": True,
        "name": "MAST water meter",
        "source": DOMAIN,
        "statistic_id": statistic_id(card),
        "unit_of_measurement": "m³",
    }
    if StatisticMeanType is not None:
        meta["mean_type"] = StatisticMeanType.NONE
    else:  # pragma: no cover
        meta["has_mean"] = False
    if "unit_class" in getattr(StatisticMetaData, "__annotations__", {}):
        meta["unit_class"] = "volume"
    return meta  # type: ignore[return-value]


class MastCoordinator(DataUpdateCoordinator[MastData]):
    """Fetch readings every few hours and import them as statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MastClient,
        card: str,
        token: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client
        self._card = card
        self._expiry = token_expiry(token)
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{card}")
        self._chunk_days = CHUNK_DAYS
        self._renewals = 0
        self._rejected_resume_credential: str | None = None
        self.auth_failures = 0  # consecutive rejections of a not-yet-expired token
        self.last_auth_error: str | None = None

    def _token_alive(self) -> bool:
        return self._expiry is not None and self._expiry > dt_util.utcnow()

    def _auth_exception(self, err: Exception) -> Exception:
        """Decide how to react when MAST refuses the token.

        A token past its own expiry needs a new login straight away. One that should still be
        valid is retried later a few times first. The very first refresh (setup) fails fast so
        that a bad token is reported immediately.
        """
        self.auth_failures += 1
        self.last_auth_error = f"{dt_util.now():%Y-%m-%d %H:%M} {err}"
        if (
            self._token_alive()
            and self.data is not None
            and self.auth_failures < AUTH_STRIKES
        ):
            _LOGGER.warning(
                "MAST refused a token that has not expired (%s), retrying in %d minutes (%d/%d)",
                err,
                AUTH_RETRY_SECONDS // 60,
                self.auth_failures,
                AUTH_STRIKES,
            )
            return UpdateFailed(
                f"MAST refused a token that has not expired ({err})",
                retry_after=AUTH_RETRY_SECONDS,
            )
        return ConfigEntryAuthFailed(f"MAST rejected the token ({err})")

    def _persist_renewed_token(self) -> None:
        """Persist the JWT and any rotated remembered-login credential together."""
        entry = self.config_entry
        current = self._client.token
        user_token = self._client.user_token
        changed = current != entry.data.get(CONF_TOKEN)
        if not changed and user_token == entry.data.get(CONF_USER_TOKEN, ""):
            return
        self._expiry = token_expiry(current)
        if changed:
            self._renewals += 1
        data = {**entry.data, CONF_TOKEN: current}
        if user_token:
            data[CONF_USER_TOKEN] = user_token
        self.hass.config_entries.async_update_entry(entry, data=data)
        if changed:
            _LOGGER.info("MAST renewed the session token (expires %s)", self._expiry)

    async def _resume_session(self) -> None:
        """Remember rejected credentials so a valid JWT can keep working quietly."""
        try:
            await self._client.async_resume_session()
        except MastAuthError:
            self._rejected_resume_credential = self._client.user_token
            raise
        else:
            self._rejected_resume_credential = None

    async def _renew_if_needed(self) -> None:
        """Renew near expiry; temporary outages must not force another OTP login."""
        now = dt_util.utcnow()
        if (
            self._client.user_token
            and (self._expiry is None or self._expiry <= now + TOKEN_RENEWAL_WINDOW)
            and not (
                self._token_alive()
                and self._client.user_token == self._rejected_resume_credential
            )
        ):
            try:
                await self._resume_session()
            except MastAuthError as err:
                if not self._token_alive():
                    raise ConfigEntryAuthFailed(
                        "MAST session-resume credential rejected"
                    ) from err
                _LOGGER.warning(
                    "MAST session resume rejected; the current JWT is still valid"
                )
            except (MastConnectionError, MastApiError) as err:
                if not self._token_alive():
                    raise UpdateFailed(
                        "MAST session renewal is temporarily unavailable"
                    ) from err
                _LOGGER.warning(
                    "MAST session renewal unavailable; retrying at the next poll"
                )
            finally:
                self._persist_renewed_token()
        if self._expiry is not None and self._expiry <= now:
            raise ConfigEntryAuthFailed("MAST token has expired")

    async def _get_readings(self, start: date, end: date) -> list[Reading]:
        """Fetch a range, repairing the backend session or renewing a rejected JWT."""
        try:
            return await self._client.async_get_readings(start, end)
        except MastAuthError as err:
            if not self._token_alive() and not self._client.user_token:
                raise
            if self._token_alive():
                try:
                    restored = await self._client.async_restore_session()
                except MastAuthError:
                    # The JWT may be rejected by both endpoints. The resume
                    # credential can still obtain a fresh one below.
                    restored = False
                _LOGGER.info(
                    "MAST refused a token that has not expired (%s); %s the session, trying again",
                    err,
                    "restored" if restored else "could not restore",
                )
                try:
                    return await self._client.async_get_readings(start, end)
                except MastAuthError:
                    if not self._client.user_token:
                        raise
            # A server-side rejection can precede the JWT's advertised expiry,
            # or expiry can occur while fetching a long backfill. Try the real
            # remembered-login flow once before asking for another browser login.
            if (
                self._token_alive()
                and self._client.user_token == self._rejected_resume_credential
            ):
                raise MastAuthError("MAST session-resume credential was rejected")
            try:
                await self._resume_session()
            finally:
                self._persist_renewed_token()
            await self._client.async_restore_session()
            return await self._client.async_get_readings(start, end)

    async def _fetch(self, start: date, end: date) -> list[Reading]:
        """Fetch [start, end] in chunks. Old failing chunks are skipped."""
        readings: list[Reading] = []
        cursor = start
        last_error: Exception | None = None
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=self._chunk_days - 1), end)
            try:
                readings += await self._get_readings(cursor, chunk_end)
                last_error = None
            except MastAuthError as err:
                raise self._auth_exception(err) from err
            except MastConnectionError as err:
                raise UpdateFailed(f"Cannot reach MAST: {err}") from err
            except MastApiError as err:
                if err.status == 404:
                    last_error = (
                        None  # the web app treats 404 as "no data in this range"
                    )
                elif err.status == 400 and self._chunk_days > SMALL_CHUNK_DAYS:
                    # This water corporation limits requests to a shorter range.
                    _LOGGER.info(
                        "MAST rejected a %d-day range, using %d days",
                        self._chunk_days,
                        SMALL_CHUNK_DAYS,
                    )
                    self._chunk_days = SMALL_CHUNK_DAYS
                    continue  # retry the same range in smaller pieces
                else:
                    _LOGGER.warning(
                        "MAST range %s..%s failed: %s", cursor, chunk_end, err
                    )
                    last_error = err
            cursor = chunk_end + timedelta(days=1)
        if last_error is not None:
            # The most recent chunk failed, so there is nothing trustworthy.
            raise UpdateFailed(f"MAST API error: {last_error}")
        return readings

    async def _async_update_data(self) -> MastData:
        await self._renew_if_needed()

        stored = await self._store.async_load() or {}
        anchor_day = date.fromisoformat(stored["day"]) if stored.get("day") else None
        today = dt_util.now().date()
        start = anchor_day if anchor_day else today - timedelta(days=BACKFILL_DAYS)

        try:
            fetched = await self._fetch(start, today)
        finally:
            self._persist_renewed_token()
        # Chunk boundaries (or the API itself) may repeat a day: keep one per day.
        by_day = {r.day: r for r in fetched}
        readings = [by_day[d] for d in sorted(by_day)]
        if not readings:
            raise UpdateFailed("MAST returned no readings")

        rows, a_reading, a_sum, a_day = build_rows(
            readings,
            dt_util.DEFAULT_TIME_ZONE,
            stored.get("reading"),
            stored.get("sum", 0.0),
            anchor_day,
        )
        if rows:
            async_add_external_statistics(
                self.hass,
                _metadata(self._card),
                [StatisticData(start=r.start, state=r.state, sum=r.sum) for r in rows],
            )
            await self._store.async_save(
                {"reading": a_reading, "sum": a_sum, "day": a_day.isoformat()}
            )
            _LOGGER.debug("Imported %d statistics rows up to %s", len(rows), a_day)

        self.auth_failures = 0
        self.last_auth_error = None
        _LOGGER.debug(
            "MAST poll ok: %d readings, latest %s", len(readings), readings[-1].day
        )
        latest = readings[-1]
        usage = (
            round(latest.value - readings[-2].value, 3) if len(readings) > 1 else None
        )
        return MastData(
            latest.value,
            latest.day,
            usage,
            self._expiry,
            token_issued(self._client.token),
            self._renewals,
        )

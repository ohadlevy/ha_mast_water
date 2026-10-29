"""Data coordinator: polls MAST and imports long-term statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
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
    STATISTICS_REPAIR_VERSION,
    STORE_VERSION,
    TOKEN_RENEWAL_WINDOW,
    UPDATE_INTERVAL,
)
from .helpers import Reading, build_rows, token_expiry, token_issued

_LOGGER = logging.getLogger(__name__)
_STATISTIC_TOLERANCE = 0.000001  # build_rows rounds sums to six decimal places

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

    async def _fetch(
        self,
        start: date,
        end: date,
        *,
        strict: bool = False,
    ) -> list[Reading]:
        """Fetch [start, end) in chunks. Old failing chunks are skipped."""
        readings: list[Reading] = []
        cursor = start
        last_error: Exception | None = None
        while cursor < end:
            # MAST excludes dateTo. Adjacent windows must therefore share a
            # boundary, and the final request must not extend past today.
            chunk_end = min(cursor + timedelta(days=self._chunk_days), end)
            try:
                chunk_readings = await self._get_readings(cursor, chunk_end)
                readings += chunk_readings
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
                    if strict:
                        raise UpdateFailed(f"MAST API error: {err}") from err
                    _LOGGER.warning(
                        "MAST range %s..%s failed: %s", cursor, chunk_end, err
                    )
                    last_error = err
            cursor = chunk_end
        if last_error is not None:
            # The most recent chunk failed, so there is nothing trustworthy.
            raise UpdateFailed(f"MAST API error: {last_error}")
        return readings

    async def _statistic_rows(
        self, start_day: date, end_day: date
    ) -> list[StatisticsRow]:
        """Read saved hourly statistics; row starts are Unix timestamps."""
        tz = dt_util.DEFAULT_TIME_ZONE
        start = datetime.combine(start_day, time.min, tzinfo=tz)
        end = datetime.combine(end_day, time.min, tzinfo=tz)
        stat_id = statistic_id(self._card)
        found = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {stat_id},
            "hour",
            None,
            {"state", "sum"},
        )
        return found.get(stat_id, [])

    async def _repair_historical_statistics(self, stored: dict, today: date) -> None:
        """Reimport old rows once, filling days lost at exclusive dateTo boundaries.

        Anchor the reconstructed sums at the last value already stored so the
        Energy dashboard's running total does not jump during this repair.
        """
        anchor_day = date.fromisoformat(stored["day"])
        if anchor_day >= today:
            raise UpdateFailed("Cannot repair history before the next meter day")
        existing = await self._statistic_rows(
            today - timedelta(days=BACKFILL_DAYS), anchor_day + timedelta(days=1)
        )
        if not existing:
            raise UpdateFailed("No saved MAST statistics to anchor the repair")
        first_day = datetime.fromtimestamp(
            min(row["start"] for row in existing), dt_util.DEFAULT_TIME_ZONE
        ).date()
        fetched = await self._fetch(
            first_day,
            anchor_day + timedelta(days=1),
            strict=True,
        )
        by_day = {reading.day: reading for reading in fetched}
        existing_days = {
            datetime.fromtimestamp(row["start"], dt_util.DEFAULT_TIME_ZONE).date()
            for row in existing
        }
        if not existing_days.issubset(by_day):
            raise UpdateFailed("MAST omitted previously imported meter days")
        readings = [by_day[day] for day in sorted(by_day) if day <= anchor_day]
        if not readings or readings[-1].day != anchor_day:
            raise UpdateFailed("MAST did not return the stored anchor day")
        if abs(readings[-1].value - stored["reading"]) > _STATISTIC_TOLERANCE:
            raise UpdateFailed("MAST changed the stored anchor reading")

        first_sum = stored["sum"]
        for index in range(len(readings) - 1, 0, -1):
            delta = readings[index].value - readings[index - 1].value
            first_sum -= max(delta, 0.0)
        if first_sum < -_STATISTIC_TOLERANCE:
            raise UpdateFailed("MAST history cannot be reconciled with the saved total")
        rows, _, _, _ = build_rows(
            readings,
            dt_util.DEFAULT_TIME_ZONE,
            readings[0].value,
            first_sum,
            None,
        )
        async_add_external_statistics(
            self.hass,
            _metadata(self._card),
            [
                StatisticData(start=row.start, state=row.state, sum=row.sum)
                for row in rows
            ],
        )
        await get_instance(self.hass).async_block_till_done()
        imported = {
            row["start"]: row
            for row in await self._statistic_rows(
                first_day, anchor_day + timedelta(days=1)
            )
        }
        for row in rows:
            saved = imported.get(row.start.timestamp())
            if (
                saved is None
                or saved.get("state") is None
                or saved.get("sum") is None
                or abs(saved["state"] - row.state) > _STATISTIC_TOLERANCE
                or abs(saved["sum"] - row.sum) > _STATISTIC_TOLERANCE
            ):
                raise UpdateFailed(
                    f"MAST statistic for {row.start.date()} was not saved"
                )
        _LOGGER.info(
            "Reimported %d MAST statistics rows to repair date boundaries", len(rows)
        )

    async def _async_update_data(self) -> MastData:
        await self._renew_if_needed()

        stored = await self._store.async_load() or {}
        anchor_day = date.fromisoformat(stored["day"]) if stored.get("day") else None
        today = dt_util.now().date()
        if (
            anchor_day
            and not stored.get("exclusive_end_repaired")
            and (
                stored.get("exclusive_end_repair_attempt_day") != today.isoformat()
                or stored.get("exclusive_end_repair_version")
                != STATISTICS_REPAIR_VERSION
            )
        ):
            stored["exclusive_end_repair_attempt_day"] = today.isoformat()
            stored["exclusive_end_repair_version"] = STATISTICS_REPAIR_VERSION
            await self._store.async_save(stored)
            try:
                await self._repair_historical_statistics(stored, today)
            except UpdateFailed as err:
                _LOGGER.warning("Could not repair old MAST statistics yet: %s", err)
            except Exception:
                _LOGGER.exception("Unexpected error repairing old MAST statistics")
            else:
                stored.pop("exclusive_end_repair_attempt_day", None)
                stored["exclusive_end_repaired"] = True
                await self._store.async_save(stored)
        # Include the previous daily value so the usage sensor can still compute
        # a delta when the saved anchor is already the latest available reading.
        start = (
            anchor_day - timedelta(days=1)
            if anchor_day
            else today - timedelta(days=BACKFILL_DAYS)
        )

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
            if anchor_day is None:
                stored["exclusive_end_repaired"] = True
            async_add_external_statistics(
                self.hass,
                _metadata(self._card),
                [StatisticData(start=r.start, state=r.state, sum=r.sum) for r in rows],
            )
            await self._store.async_save(
                {
                    **stored,
                    "reading": a_reading,
                    "sum": a_sum,
                    "day": a_day.isoformat(),
                }
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

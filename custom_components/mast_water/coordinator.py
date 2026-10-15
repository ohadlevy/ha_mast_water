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
    BACKFILL_DAYS,
    CHUNK_DAYS,
    CONF_TOKEN,
    SMALL_CHUNK_DAYS,
    DOMAIN,
    STORE_VERSION,
    UPDATE_INTERVAL,
)
from .helpers import Reading, build_rows, token_expiry

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

    def _persist_renewed_token(self) -> None:
        """Store a token MAST renewed via the `new-token` response header."""
        entry = self.config_entry
        current = self._client.token
        if current == entry.data.get(CONF_TOKEN):
            return
        self._expiry = token_expiry(current)
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: current}
        )
        _LOGGER.info("MAST renewed the session token (expires %s)", self._expiry)

    async def _fetch(self, start: date, end: date) -> list[Reading]:
        """Fetch [start, end] in chunks. Old failing chunks are skipped."""
        readings: list[Reading] = []
        cursor = start
        last_error: Exception | None = None
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=self._chunk_days - 1), end)
            try:
                readings += await self._client.async_get_readings(cursor, chunk_end)
                last_error = None
            except MastAuthError as err:
                raise ConfigEntryAuthFailed("MAST token rejected or expired") from err
            except MastConnectionError as err:
                raise UpdateFailed(f"Cannot reach MAST: {err}") from err
            except MastApiError as err:
                if err.status == 404:
                    last_error = None  # the web app treats 404 as "no data in this range"
                elif err.status == 400 and self._chunk_days > SMALL_CHUNK_DAYS:
                    # This water corporation limits requests to a shorter range.
                    _LOGGER.info("MAST rejected a %d-day range, using %d days", self._chunk_days, SMALL_CHUNK_DAYS)
                    self._chunk_days = SMALL_CHUNK_DAYS
                    continue  # retry the same range in smaller pieces
                else:
                    _LOGGER.warning("MAST range %s..%s failed: %s", cursor, chunk_end, err)
                    last_error = err
            cursor = chunk_end + timedelta(days=1)
        if last_error is not None:
            # The most recent chunk failed, so there is nothing trustworthy.
            raise UpdateFailed(f"MAST API error: {last_error}")
        return readings

    async def _async_update_data(self) -> MastData:
        now = dt_util.utcnow()
        if self._expiry is not None and self._expiry <= now:
            raise ConfigEntryAuthFailed("MAST token has expired")

        stored = await self._store.async_load() or {}
        anchor_day = (
            date.fromisoformat(stored["day"]) if stored.get("day") else None
        )
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

        latest = readings[-1]
        usage = (
            round(latest.value - readings[-2].value, 3) if len(readings) > 1 else None
        )
        return MastData(latest.value, latest.day, usage, self._expiry)

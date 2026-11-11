"""Pure helpers (no Home Assistant imports) so they can be tested standalone."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any


@dataclass(frozen=True)
class Reading:
    """One cumulative meter reading (m3) for a calendar day."""

    day: date
    value: float


@dataclass(frozen=True)
class Card:
    """A water card (meter) on the account."""

    number: str
    address: str
    source: str = "watermeter"


@dataclass(frozen=True)
class BillingPeriod:
    """One billed period from GetWaterCardConsumptions.

    ``end`` is the date of the closing meter reading (``lastReading``). Billing
    periods follow the water corporation's own calendar, not calendar months.
    ``shared_usage`` is the building's shared consumption allocated to this home.
    """

    end: date
    year: int | None
    period: int | None
    days: int | None
    private_usage: float | None
    shared_usage: float | None
    total_usage: float | None
    water_price: float | None
    sewage_price: float | None
    total_price: float | None


@dataclass(frozen=True)
class StatRow:
    """One hourly statistics row to import."""

    start: datetime  # timezone-aware, on the hour
    state: float
    sum: float


def clean_token(raw: str) -> str:
    """Return the bare JWT, tolerating a pasted 'Bearer ' prefix or quotes."""
    token = raw.strip().strip("'\"").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _claim_time(token: str, claim: str) -> datetime | None:
    """Read a timestamp claim from a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(claims[claim]), tz=UTC)
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def token_expiry(token: str) -> datetime | None:
    """Read the `exp` claim from a JWT without verifying it."""
    return _claim_time(token, "exp")


def token_issued(token: str) -> datetime | None:
    """Read the `iat` claim from a JWT without verifying it."""
    return _claim_time(token, "iat")


def parse_cards(payload: Any, source: str = "watermeter") -> list[Card]:
    """Parse GetWaterMeterCards into cards, skipping malformed entries."""
    if not isinstance(payload, list):
        raise ValueError("expected a list of cards")  # noqa: TRY004
    cards: list[Card] = []
    for item in payload:
        try:
            number = str(item["waterCardNumber"]).strip()
        except (KeyError, TypeError):
            continue
        if number:
            cards.append(Card(number, str(item.get("address") or "").strip(), source))
    return cards


def parse_readings(payload: Any) -> list[Reading]:
    """Parse the API response into sorted, de-duplicated daily readings."""
    if not isinstance(payload, list):
        raise ValueError("expected a list of readings")  # noqa: TRY004
    by_day: dict[date, float] = {}
    for item in payload:
        try:
            day = datetime.fromisoformat(item["readingDate"]).date()
            by_day[day] = float(item["reading"])
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed entries
    return [Reading(d, v) for d, v in sorted(by_day.items())]


def _number(value: Any) -> float | None:
    """A finite number, or None for anything else (including bools)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _whole(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _day(value: Any) -> date | None:
    """Parse an ISO timestamp or a dd/mm/yyyy date into a day."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    date_part = text.replace("T", " ").split(" ")[0]
    try:
        return datetime.strptime(date_part, "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_billing_periods(payload: Any) -> list[BillingPeriod]:
    """Parse GetWaterCardConsumptions into billed periods, oldest first.

    Entries without a usable closing-reading date are skipped, because the date
    is what places a period on the timeline.
    """
    if not isinstance(payload, list):
        raise TypeError("expected a list of billing periods")
    periods: dict[date, BillingPeriod] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        end = _day(item.get("lastReading"))
        if end is None:
            continue
        periods[end] = BillingPeriod(
            end=end,
            year=_whole(item.get("year")),
            period=_whole(item.get("period")),
            days=_whole(item.get("consumptionDays")),
            private_usage=_number(item.get("personalUsage")),
            shared_usage=_number(item.get("legacyUsage")),
            total_usage=_number(item.get("totalUsage")),
            water_price=_number(item.get("waterPrice")),
            sewage_price=_number(item.get("sewagPrice")),
            total_price=_number(item.get("totalPrice")),
        )
    return [periods[end] for end in sorted(periods)]


def billing_to_dict(period: BillingPeriod) -> dict[str, Any]:
    """Serialise a billed period for storage (JSON-safe)."""
    return {**asdict(period), "end": period.end.isoformat()}


def billing_from_dict(data: Any) -> BillingPeriod | None:
    """Rebuild a stored billed period; None if the stored entry is unusable."""
    if not isinstance(data, dict):
        return None
    end = _day(data.get("end"))
    if end is None:
        return None
    return BillingPeriod(
        end=end,
        year=_whole(data.get("year")),
        period=_whole(data.get("period")),
        days=_whole(data.get("days")),
        private_usage=_number(data.get("private_usage")),
        shared_usage=_number(data.get("shared_usage")),
        total_usage=_number(data.get("total_usage")),
        water_price=_number(data.get("water_price")),
        sewage_price=_number(data.get("sewage_price")),
        total_price=_number(data.get("total_price")),
    )


def build_rows(
    readings: list[Reading],
    tz: tzinfo,
    anchor_reading: float | None,
    anchor_sum: float,
    anchor_day: date | None,
) -> tuple[list[StatRow], float | None, float, date | None]:
    """Turn cumulative readings into statistics rows with a running sum.

    The anchor is the last reading already imported (or None on first run, in
    which case the first reading becomes the zero point). Only readings newer
    than ``anchor_day`` are emitted. A decrease (e.g. meter replacement) adds
    zero instead of a negative amount.

    Returns (rows, new_anchor_reading, new_anchor_sum, new_anchor_day).
    """
    rows: list[StatRow] = []
    prev = anchor_reading
    running = anchor_sum
    last_day = anchor_day
    for r in readings:
        if anchor_day is not None and r.day <= anchor_day:
            continue
        if prev is not None:
            delta = r.value - prev
            running += delta if delta > 0 else 0.0
        prev = r.value
        last_day = r.day
        start = datetime(r.day.year, r.day.month, r.day.day, tzinfo=tz)
        rows.append(StatRow(start=start, state=r.value, sum=round(running, 6)))
    return rows, prev, running, last_day

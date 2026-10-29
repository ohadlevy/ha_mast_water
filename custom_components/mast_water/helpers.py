"""Pure helpers (no Home Assistant imports) so they can be tested standalone."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
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


def parse_cards(payload: Any) -> list[Card]:
    """Parse GetWaterMeterCards into cards, skipping malformed entries."""
    if not isinstance(payload, list):
        raise TypeError("expected a list of cards")
    cards: list[Card] = []
    for item in payload:
        try:
            number = str(item["waterCardNumber"]).strip()
        except (KeyError, TypeError):
            continue
        if number:
            cards.append(Card(number, str(item.get("address") or "").strip()))
    return cards


def parse_readings(payload: Any) -> list[Reading]:
    """Parse the API response into sorted, de-duplicated daily readings."""
    if not isinstance(payload, list):
        raise TypeError("expected a list of readings")
    by_day: dict[date, float] = {}
    for item in payload:
        try:
            day = datetime.fromisoformat(item["readingDate"]).date()
            by_day[day] = float(item["reading"])
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed entries
    return [Reading(d, v) for d, v in sorted(by_day.items())]


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

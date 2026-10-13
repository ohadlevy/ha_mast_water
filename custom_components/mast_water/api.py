"""Async client for the MAST water meter API (unofficial)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import aiohttp

from .const import API_URL, CARDS_URL, ORIGIN
from .helpers import Card, Reading, parse_cards, parse_readings

_LOGGER = logging.getLogger(__name__)


class MastError(Exception):
    """Base error."""


class MastAuthError(MastError):
    """The token was rejected or has expired."""


class MastApiError(MastError):
    """The API returned an error or an unexpected response."""


class MastConnectionError(MastError):
    """Could not reach the API."""


class MastClient:
    """Thin client around the two MAST endpoints this integration needs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        card_number: str,
        company_id: str,
        url: str = API_URL,
        cards_url: str = CARDS_URL,
    ) -> None:
        self._session = session
        self._token = token
        self._card = card_number
        self._company = company_id
        self._url = url
        self._cards_url = cards_url

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "jwt": f"Bearer {self._token}",
            "X-CompanyId": self._company,
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "User-Agent": "Mozilla/5.0 (compatible; HomeAssistant mast_water)",
        }

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send a request and return the decoded JSON body."""
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as resp:
                status = resp.status
                text = await resp.text()
                if status in (401, 403):
                    raise MastAuthError(f"HTTP {status}")
                if status >= 400:
                    # The API answers bad card numbers / bad ranges with a
                    # generic 500 and a Hebrew message, so keep the status.
                    _LOGGER.debug("MAST API HTTP %s: %s", status, text[:200])
                    raise MastApiError(f"HTTP {status}")
                try:
                    return await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise MastApiError("response was not JSON") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MastConnectionError(str(err)) from err

    async def async_get_readings(self, date_from: date, date_to: date) -> list[Reading]:
        """Fetch cumulative daily readings for an inclusive date range."""
        payload = await self._request(
            "POST",
            self._url,
            json={
                "waterCardNumber": self._card,
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
            },
        )
        try:
            return parse_readings(payload)
        except ValueError as err:
            raise MastApiError(f"unexpected response: {err}") from err

    async def async_get_cards(self) -> list[Card]:
        """List the water cards (meters) on the logged-in account."""
        payload = await self._request("GET", self._cards_url)
        try:
            return parse_cards(payload)
        except ValueError as err:
            raise MastApiError(f"unexpected response: {err}") from err

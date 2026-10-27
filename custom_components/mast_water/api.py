"""Async client for the MAST water meter API (unofficial)."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import aiohttp

from .const import API_URL, CARDS_URL, ORIGIN, VALIDATE_URL
from .helpers import Card, Reading, clean_token, parse_cards, parse_readings, token_expiry

_LOGGER = logging.getLogger(__name__)


class MastError(Exception):
    """Base error."""


class MastAuthError(MastError):
    """The token was rejected or has expired."""


class MastApiError(MastError):
    """The API returned an error or an unexpected response."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MastConnectionError(MastError):
    """Could not reach the API."""


def error_reason(text: str) -> str | None:
    """Pull the human-readable error out of a MAST JSON error body, if there is one.

    The web app reads `reason` from 401 bodies (for example `"expired"`); other
    errors carry a `message`.
    """
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if isinstance(body, dict):
        for key in ("reason", "message"):
            if body.get(key):
                return str(body[key])[:200]
    return None


class MastClient:
    """Thin client around the MAST endpoints this integration needs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        card_number: str,
        company_id: str,
        url: str = API_URL,
        cards_url: str = CARDS_URL,
        validate_url: str = VALIDATE_URL,
    ) -> None:
        self._session = session
        self._token = token
        self._card = card_number
        self._company = company_id
        self._url = url
        self._cards_url = cards_url
        self._validate_url = validate_url

    @property
    def token(self) -> str:
        """The token currently in use (may be newer than the one passed in)."""
        return self._token

    def _adopt_new_token(self, header_value: str | None) -> None:
        """Take over a renewed token the server sends in the `new-token` header.

        The MAST web app does exactly this on every response (its HTTP
        interceptor replaces the stored token when the header is present).
        Only a well-formed JWT that outlives the current one is accepted.
        """
        if not header_value:
            return
        candidate = clean_token(header_value)
        new_exp = token_expiry(candidate)
        if new_exp is None or candidate == self._token:
            return
        current_exp = token_expiry(self._token)
        if current_exp is None or new_exp > current_exp:
            _LOGGER.debug("MAST sent a renewed token (expires %s)", new_exp)
            self._token = candidate

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
                    reason = error_reason(text)
                    if status == 403 and reason is None:
                        # A 403 without MAST's JSON error body (for example an HTML
                        # page from the CDN in front of the API) is a block, not an
                        # expired token, so do not ask the user to log in again.
                        raise MastApiError("HTTP 403 (blocked, not a MAST error)", status)
                    detail = f", reason: {reason}" if reason else ""
                    raise MastAuthError(f"HTTP {status}{detail}")
                if status >= 400:
                    # The API answers bad card numbers / bad ranges with a
                    # generic 500 and a Hebrew message, so keep the status.
                    _LOGGER.debug("MAST API HTTP %s: %s", status, text[:200])
                    raise MastApiError(f"HTTP {status}", status)
                self._adopt_new_token(resp.headers.get("new-token"))
                try:
                    return await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise MastApiError("response was not JSON") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MastConnectionError(str(err)) from err

    async def async_restore_session(self) -> bool:
        """Do what the web app does before it opens the water page.

        A few hours after login MAST drops the server-side state behind the WaterMeter
        endpoints, which then answer 401 `Expired|NotValid` although the token is still
        valid. Validating the user's assets and then loading the water cards, in that
        order, brings it back. Returns False if either call fails or MAST reports the
        assets as not valid.
        """
        try:
            validation = await self._request("GET", self._validate_url)
            await self._request("GET", self._cards_url)
        except MastError as err:
            _LOGGER.debug("Could not restore the MAST session: %s", err)
            return False
        return not (isinstance(validation, dict) and validation.get("isValid") is False)

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

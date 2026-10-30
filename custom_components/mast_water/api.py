"""Async client for the MAST water meter API (unofficial)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import aiohttp

from .const import API_URL, CARDS_URL, CITIES_URL, ORIGIN, RESUME_URL, VALIDATE_URL
from .helpers import (
    Card,
    Reading,
    clean_token,
    parse_cards,
    parse_readings,
    token_expiry,
)

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
        *,
        user_token: str = "",
        resume_url: str = RESUME_URL,
        cities_url: str = CITIES_URL,
    ) -> None:
        self._session = session
        self._token = token
        self._card = card_number
        self._company = company_id
        self._url = url
        self._cards_url = cards_url
        self._validate_url = validate_url
        self._user_token = user_token
        self._resume_url = resume_url
        self._cities_url = cities_url

    @property
    def token(self) -> str:
        """The token currently in use (may be newer than the one passed in)."""
        return self._token

    @property
    def user_token(self) -> str:
        """The opaque session-resume credential, including any server rotation."""
        return self._user_token

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
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-CompanyId": self._company,
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "User-Agent": "Mozilla/5.0 (compatible; HomeAssistant mast_water)",
        }
        if self._token:
            headers["jwt"] = f"Bearer {self._token}"
        return headers

    async def _request(
        self, method: str, url: str, *, resume: bool = False, **kwargs: Any
    ) -> Any:
        """Send a request and return the decoded JSON body."""
        headers = self._headers()
        if resume:
            # The app's remembered-login credential works independently of the
            # old JWT. In particular, never attach an expired JWT to this call.
            headers.pop("jwt", None)
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
                **kwargs,
            ) as resp:
                status = resp.status
                text = await resp.text()
                if status in (401, 403):
                    if resume:
                        # Error bodies and exception URLs may echo the query
                        # credential. Do not put them in logs or exceptions.
                        if status == 403 and error_reason(text) is None:
                            raise MastApiError(
                                "HTTP 403 (session resume blocked)", status
                            )
                        raise MastAuthError("MAST session resume rejected")
                    reason = error_reason(text)
                    if status == 403 and reason is None:
                        # A 403 without MAST's JSON error body (for example an HTML
                        # page from the CDN in front of the API) is a block, not an
                        # expired token, so do not ask the user to log in again.
                        raise MastApiError(
                            "HTTP 403 (blocked, not a MAST error)", status
                        )
                    detail = f", reason: {reason}" if reason else ""
                    raise MastAuthError(f"HTTP {status}{detail}")
                if status >= 300:
                    # The API answers bad card numbers / bad ranges with a
                    # generic 500 and a Hebrew message, so keep the status.
                    if not resume:
                        _LOGGER.debug("MAST API HTTP %s: %s", status, text[:200])
                    raise MastApiError(f"HTTP {status}", status)
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    raise MastApiError("response was not JSON") from None
                candidate = resp.headers.get("new-token")
                if resume:
                    if (
                        not isinstance(payload, dict)
                        or payload.get("isSuccess") is not True
                    ):
                        raise MastAuthError("MAST session resume rejected")
                    expiry = token_expiry(clean_token(candidate or ""))
                    if expiry is None or expiry <= datetime.now(UTC):
                        raise MastApiError("Session resume returned no valid JWT")
                    user = payload.get("user")
                    returned = user.get("token") if isinstance(user, dict) else None
                    if isinstance(returned, str) and returned.strip():
                        self._user_token = returned.strip()
                self._adopt_new_token(candidate)
                return payload
        except (aiohttp.ClientError, TimeoutError) as err:
            if resume:
                raise MastConnectionError("Cannot reach MAST session resume") from None
            raise MastConnectionError(str(err)) from err

    async def async_resume_session(self) -> None:
        """Exchange the remembered-login token for a JWT without a new OTP."""
        if not self._user_token:
            raise MastAuthError("No MAST session-resume credential configured")
        await self._request(
            "GET", self._resume_url, resume=True, params={"token": self._user_token}
        )

    async def async_restore_session(self) -> bool:
        """Do what the web app does before it opens the water page.

        A few hours after login MAST drops the server-side state behind the WaterMeter
        endpoints, which then answer 401 `Expired|NotValid` although the token is still
        valid. Validating the user's assets and then loading the water cards, in that
        order, brings it back. Returns False only when MAST reports the assets
        as invalid. Request failures retain their own error type.
        """
        validation = await self._request("GET", self._validate_url)
        await self._request("GET", self._cards_url)
        return not (isinstance(validation, dict) and validation.get("isValid") is False)

    async def async_get_readings(self, date_from: date, date_to: date) -> list[Reading]:
        """Fetch cumulative daily readings for [date_from, date_to)."""
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

    async def async_get_cities(self) -> list[dict[str, Any]]:
        """Fetch available water corporations and cities."""
        payload = await self._request("GET", self._cities_url)
        if not isinstance(payload, dict):
            return []
        return payload.get("companyDetailsMediaList", [])

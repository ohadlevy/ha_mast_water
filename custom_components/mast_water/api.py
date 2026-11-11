"""Async client for the MAST water meter API (unofficial)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import aiohttp

from .const import API_URL, BILLING_URL, CARDS_URL, ORIGIN, RESUME_URL, VALIDATE_URL
from .helpers import (
    BillingPeriod,
    Card,
    Reading,
    clean_token,
    parse_billing_periods,
    parse_cards,
    parse_readings,
    token_expiry,
)

_LOGGER = logging.getLogger(__name__)


class MastError(Exception):
    """Base error."""


class MastAuthError(MastError):
    """The token was rejected or has expired."""


class MastCompanyError(MastAuthError):
    """MAST does not accept the X-CompanyId for this account (401 "Company Not valid").

    A subclass of MastAuthError so existing handlers keep treating it as an auth
    failure, while the config flow can tell the user the Company ID is the problem.
    """


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


def _sanitize_log_text(text: str, secret: str = "", card: str = "") -> str:
    """Remove or redact sensitive credentials from text before logging."""
    if not text:
        return ""
    sanitized = text
    if secret:
        sanitized = sanitized.replace(secret, "[REDACTED]")
    if card and len(card) > 3:
        sanitized = sanitized.replace(card, f"[CARD...{card[-3:]}]")
    return sanitized


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
        billing_url: str = BILLING_URL,
    ) -> None:
        self._billing_url = billing_url
        self._session = session
        self._token = token
        self._card = card_number
        self._company = company_id
        self._url = url
        self._cards_url = cards_url
        self._validate_url = validate_url
        self._user_token = user_token
        self._resume_url = resume_url
        self._companies: list[dict[str, Any]] = []
        _LOGGER.debug(
            "MAST client initialized (company: %s, card: ...%s)",
            company_id,
            card_number[-3:] if card_number else "none",
        )

    @property
    def token(self) -> str:
        """The token currently in use (may be newer than the one passed in)."""
        return self._token

    @property
    def user_token(self) -> str:
        """The opaque session-resume credential, including any server rotation."""
        return self._user_token

    @property
    def company_id(self) -> str:
        """The company ID currently in use."""
        return self._company

    @property
    def companies(self) -> list[dict[str, Any]]:
        """List of companies on the account."""
        return self._companies

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
            "Referer": f"{ORIGIN}/{self._company}/services",
            "User-Agent": "Mozilla/5.0 (compatible; HomeAssistant mast_water)",
        }

    async def _request(
        self, method: str, url: str, *, resume: bool = False, **kwargs: Any
    ) -> Any:
        """Send a request and return the decoded JSON body."""
        headers = self._headers()
        if resume:
            # The app's remembered-login credential works independently of the
            # old JWT. In particular, never attach an expired JWT to this call.
            headers.pop("jwt", None)
        parsed_url = str(url).split("?")[0]
        card_info = f", card: ...{self._card[-3:]}" if self._card else ""
        _LOGGER.debug(
            "MAST API request: %s %s (company: %s, resume: %s%s)",
            method,
            parsed_url,
            self._company,
            resume,
            card_info,
        )
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
                            _LOGGER.warning(
                                "MAST API auth failure: %s %s -> HTTP 403 (session resume blocked)",
                                method,
                                parsed_url,
                            )
                            raise MastApiError(
                                "HTTP 403 (session resume blocked)", status
                            )
                        _LOGGER.warning(
                            "MAST API auth failure: %s %s -> HTTP %s (session resume rejected)",
                            method,
                            parsed_url,
                            status,
                        )
                        raise MastAuthError("MAST session resume rejected")
                    reason = error_reason(text)
                    if status == 403 and reason is None:
                        # A 403 without MAST's JSON error body (for example an HTML
                        # page from the CDN in front of the API) is a block, not an
                        # expired token, so do not ask the user to log in again.
                        _LOGGER.warning(
                            "MAST API auth failure: %s %s -> HTTP 403 (blocked, not a MAST error)",
                            method,
                            parsed_url,
                        )
                        raise MastApiError(
                            "HTTP 403 (blocked, not a MAST error)", status
                        )
                    detail = f", reason: {reason}" if reason else ""
                    body_snippet = _sanitize_log_text(
                        text, self._user_token, self._card
                    )
                    body_info = f", body: {body_snippet}" if body_snippet else ""
                    _LOGGER.warning(
                        "MAST API auth failure: %s %s -> HTTP %s%s%s",
                        method,
                        parsed_url,
                        status,
                        detail,
                        body_info,
                    )
                    if reason and "company not valid" in reason.lower():
                        raise MastCompanyError(f"HTTP {status}{detail}")
                    raise MastAuthError(f"HTTP {status}{detail}")
                if status >= 300:
                    # The API answers bad card numbers / bad ranges with a
                    # generic 500 and a Hebrew message, so keep the status.
                    _LOGGER.warning(
                        "MAST API HTTP error: %s %s -> HTTP %s: %s",
                        method,
                        parsed_url,
                        status,
                        _sanitize_log_text(text, self._user_token, self._card),
                    )
                    raise MastApiError(f"HTTP {status}", status)
                _LOGGER.debug(
                    "MAST API success: %s %s -> HTTP %s",
                    method,
                    parsed_url,
                    status,
                )
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    _LOGGER.warning(
                        "MAST API response error: %s %s -> response was not JSON",
                        method,
                        parsed_url,
                    )
                    raise MastApiError("response was not JSON") from None

                if _LOGGER.isEnabledFor(logging.DEBUG):
                    if isinstance(payload, list):
                        structure = f"list[{len(payload)}]"
                        if payload and isinstance(payload[0], dict):
                            structure += f" with item keys: {sorted(payload[0].keys())}"
                    elif isinstance(payload, dict):
                        keys = sorted(payload.keys())
                        safe = {
                            k: v
                            for k, v in payload.items()
                            if k
                            in ("companyId", "isActiveCompany", "isValid", "isSuccess")
                            and isinstance(v, (str, int, bool))
                        }
                        # Never log values from the user object, only its presence and keys.
                        user_info = ""
                        if "user" in payload and isinstance(payload["user"], dict):
                            user_info = f", user keys: {sorted(payload['user'].keys())}"
                        structure = f"dict with keys {keys}{user_info}"
                        if safe:
                            structure += f", values: {safe}"
                    else:
                        structure = type(payload).__name__
                    _LOGGER.debug("MAST API response structure: %s", structure)

                candidate = resp.headers.get("new-token")
                if resume:
                    if (
                        not isinstance(payload, dict)
                        or payload.get("isSuccess") is not True
                    ):
                        _LOGGER.warning(
                            "MAST API session resume rejected in payload: %s %s",
                            method,
                            parsed_url,
                        )
                        raise MastAuthError("MAST session resume rejected")
                    expiry = token_expiry(clean_token(candidate or ""))
                    if expiry is None or expiry <= datetime.now(UTC):
                        _LOGGER.warning(
                            "MAST API session resume returned no valid JWT: %s %s",
                            method,
                            parsed_url,
                        )
                        raise MastApiError("Session resume returned no valid JWT")
                    user = payload.get("user")
                    if isinstance(user, dict):
                        returned = user.get("token")
                        if isinstance(returned, str) and returned.strip():
                            self._user_token = returned.strip()
                    companies = (
                        payload.get("userCompanies")
                        or payload.get("companies")
                        or (
                            user.get("userCompanies")
                            if isinstance(user, dict)
                            else None
                        )
                        or (user.get("companies") if isinstance(user, dict) else None)
                    )
                    if isinstance(companies, list):
                        seen = set()
                        deduped = []
                        for c in companies:
                            if isinstance(c, dict):
                                cid = c.get("companyId")
                                if cid is not None and cid not in seen:
                                    seen.add(cid)
                                    deduped.append(c)
                        self._companies = deduped
                        active = next(
                            (c for c in self._companies if c.get("isActiveCompany")),
                            self._companies[0] if self._companies else None,
                        )
                        cid = (
                            active.get("companyId")
                            if isinstance(active, dict)
                            else None
                        )
                        if (
                            cid is not None
                            and str(cid).strip()
                            and str(cid).strip() != "0"
                        ):
                            cid_str = str(cid).strip()
                            if cid_str != self._company:
                                _LOGGER.info(
                                    "MAST adopted company %s from account (previously %s)",
                                    cid_str,
                                    self._company,
                                )
                                self._company = cid_str
                self._adopt_new_token(candidate)
                return payload
        except (aiohttp.ClientError, TimeoutError) as err:
            if resume:
                _LOGGER.warning(
                    "MAST API connection error (resume): %s %s -> %s",
                    method,
                    parsed_url,
                    err,
                )
                raise MastConnectionError("Cannot reach MAST session resume") from None
            _LOGGER.warning(
                "MAST API connection error: %s %s -> %s",
                method,
                parsed_url,
                err,
            )
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
        await self.async_get_cards()
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

    async def async_get_billing_periods(self) -> list[BillingPeriod]:
        """Fetch the billed periods of the card, oldest first.

        This is the table the MAST web app shows under the water card: one row per
        billing period with private and shared consumption, days and cost.
        """
        payload = await self._request(
            "GET", self._billing_url, params={"waterCard": self._card}
        )
        try:
            return parse_billing_periods(payload)
        except (TypeError, ValueError) as err:
            raise MastApiError(f"unexpected response: {err}") from err

    async def async_get_cards(self) -> list[Card]:
        """List the water cards (meters) on the logged-in account."""
        try:
            payload = await self._request("GET", self._cards_url)
            return parse_cards(payload, source="watermeter")
        except MastCompanyError:
            alt_url = str(self._cards_url).replace(
                "WaterMeter/GetWaterMeterCards",
                "PersonalAreaAsset/GetPayerWaterCards",
            )
            payload = await self._request("GET", alt_url)
            return parse_cards(payload, source="payer")

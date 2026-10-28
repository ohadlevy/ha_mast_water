#!/usr/bin/env python3
"""Probe MAST's observed app session-resume flow without printing credentials.

Input JSON: {"user_token": "...", "jwt_token": "..."}. The JWT is optional
and used only for comparing expiry; it is never sent on the resume request.
This is an investigation tool, not an enabled integration authentication flow.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_BASE = "https://api.mast.co.il/mast/api/"


class NoRedirect(HTTPRedirectHandler):
    """Keep the query credential on the observed API endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bare_jwt(value: str) -> str:
    value = value.strip()
    return value[7:].strip() if value.lower().startswith("bearer ") else value


def jwt_expiry(value: str) -> int | None:
    """Read only expiry metadata; this does not verify a JWT signature."""
    try:
        part = bare_jwt(value).split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        expiry = payload["exp"]
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            return None
        datetime.fromtimestamp(expiry, timezone.utc)
        return int(expiry)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError):
        return None


def iso_expiry(expiry: int | None) -> str | None:
    return (
        datetime.fromtimestamp(expiry, timezone.utc).isoformat()
        if expiry is not None
        else None
    )


def request_json(opener, url: str, headers: dict) -> tuple[int, object, object]:
    try:
        response = opener.open(Request(url, headers=headers), timeout=30)
    except HTTPError as err:
        response = err
    with response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("response_size")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = None
        return response.code, response.headers, body


def probe(credentials: dict, company_id: str, output: Path, opener=None) -> dict:
    """Resume without cookies/JWT, preserve returned tokens, then check water access."""
    user_token = credentials.get("user_token")
    if not isinstance(user_token, str) or not user_token.strip():
        raise ValueError("missing_user_token")
    old_jwt = credentials.get("jwt_token") or ""
    if not isinstance(old_jwt, str):
        raise ValueError("invalid_jwt_type")
    if output.exists():
        raise FileExistsError("output_exists")
    opener = opener or build_opener(NoRedirect())
    headers = {
        "Accept": "application/json",
        "Origin": "https://www.mast.co.il",
        "Referer": "https://www.mast.co.il/",
        "X-CompanyId": company_id,
        "User-Agent": "Mozilla/5.0 (compatible; HomeAssistant mast_water research)",
    }
    url = API_BASE + "Users/GetUserByToken?" + urlencode({"token": user_token})
    status, response_headers, body = request_json(opener, url, headers)
    success = (
        200 <= status < 300 and isinstance(body, dict) and body.get("isSuccess") is True
    )
    user = body.get("user") if isinstance(body, dict) else None
    next_user_token = user.get("token") if isinstance(user, dict) else None
    jwt = bare_jwt(response_headers.get("new-token", ""))
    old_expiry, new_expiry = jwt_expiry(old_jwt), jwt_expiry(jwt)
    result = {
        "resume_http_status": status,
        "resume_success": success,
        "new_token_header_present": bool(jwt),
        "previous_jwt_expires": iso_expiry(old_expiry),
        "returned_jwt_expires": iso_expiry(new_expiry),
        "jwt_expiry_extended": new_expiry > old_expiry
        if new_expiry is not None and old_expiry is not None
        else None,
        "user_token_returned": isinstance(next_user_token, str)
        and bool(next_user_token),
        "user_token_changed": bool(next_user_token) and next_user_token != user_token,
    }
    if success:
        # Preserve possible rotation before doing the water access checks. Never
        # overwrite an existing credentials file, and never print the response.
        with os.fdopen(
            os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as handle:
            json.dump(
                {
                    "user_token": next_user_token or user_token,
                    "jwt_token": jwt if new_expiry is not None else old_jwt,
                },
                handle,
            )
        result["saved_session_file"] = str(output)
    if success and new_expiry is not None:
        authenticated_headers = {**headers, "jwt": "Bearer " + jwt}
        status, _, validation = request_json(
            opener,
            API_BASE + "PersonalAreaAsset/ValidateUserAssets",
            authenticated_headers,
        )
        result["asset_validation_http_status"] = status
        result["assets_valid"] = (
            validation.get("isValid") if isinstance(validation, dict) else None
        )
        status, _, cards = request_json(
            opener, API_BASE + "WaterMeter/GetWaterMeterCards", authenticated_headers
        )
        result["water_cards_http_status"] = status
        result["water_card_count"] = len(cards) if isinstance(cards, list) else None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument(
        "--output-file",
        type=Path,
        required=True,
        help="New private file for any returned credentials; must not exist",
    )
    parser.add_argument("--company-id", default="110")
    args = parser.parse_args()
    try:
        credentials = json.loads(args.credentials_file.read_text())
        if not isinstance(credentials, dict):
            raise ValueError("invalid_credentials_object")
        result = probe(credentials, args.company_id, args.output_file)
    except Exception as err:
        # Exception text, URLs, and error bodies can contain the query token.
        print(
            json.dumps(
                {
                    "probe_error_type": type(err).__name__,
                    "details": "No response or credential values printed",
                }
            )
        )
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["resume_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

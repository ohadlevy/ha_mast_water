#!/usr/bin/env python3
"""Probe the MAST API for one account: which companies it has and what each one answers.

Standard library only, so the person with the problem can run it themselves and paste the
output, and their token never has to be shared. Tokens are never printed or saved. Saved
bodies keep their structure but values are replaced with placeholders unless --raw is given.

    export MAST_USER_TOKEN='...'     # the remembered-login user.token (not the JWT)
    python3 scripts/mast_probe.py
    python3 scripts/mast_probe.py --company 12 --card 18017458 --save probe_out/
    python3 scripts/mast_probe.py --extra "GET SomeController/GetCompanyModules"

What it does:
  1. Resumes the session (GET Users/GetUserByToken, no JWT), like the integration does,
     once per X-CompanyId variant (no header, a company that cannot exist, and any --company).
     This shows whether the header matters for resume at all.
  2. Lists every company the response mentions and which one MAST marks active.
  3. For each company on the account (plus any --company or --control given) calls the
     endpoints the integration uses, plus the resident-session ones from docs/FINDINGS.md,
     and prints one line per call and a status matrix at the end.

Only the account's own companies are probed by default. Another corporation's company id
(for example --control 110) only produces session errors that say nothing about the account.

The methods and parameters of the "unverified" endpoints are guesses taken from FINDINGS.md.
A 404 or 405 there means "guessed wrong", not "company refused". Copy the real path from the
browser's DevTools and pass it with --extra.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_BASE = os.environ.get("MAST_API_BASE", "https://api.mast.co.il/mast/api/")
ORIGIN = "https://www.mast.co.il"

# Values under these keys are safe to keep when sanitizing a body.
SAFE_KEYS = {
    "companyid",
    "companyname",
    "isactivecompany",
    "issuccess",
    "isvalid",
    "reason",
    "readingdate",
    "reading",
}

SECRETS: list[str] = []


# --------------------------------------------------------------------------- helpers


def scrub(text: str) -> str:
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def clean_token(raw: str) -> str:
    token = raw.strip().strip("'\"").strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError):
        return {}


def sanitize(node, key: str = ""):
    """Keep the structure, drop the values (except a short whitelist and booleans)."""
    if isinstance(node, dict):
        return {k: sanitize(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [sanitize(v, key) for v in node[:3]]
    if node is None or isinstance(node, bool) or key.lower() in SAFE_KEYS:
        return node
    if isinstance(node, str):
        return f"<str len={len(node)}>"
    return "<number>"


def describe(payload) -> str:
    if isinstance(payload, dict):
        keys = list(payload)
        return "object keys=" + ",".join(keys[:12]) + (",..." if len(keys) > 12 else "")
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            keys = list(payload[0])
            text = f"list[{len(payload)}] of objects keys=" + ",".join(keys[:10])
            days = sorted(
                str(p["readingDate"])[:10]
                for p in payload
                if isinstance(p, dict) and p.get("readingDate")
            )
            if days:
                text += f" ({days[0]} .. {days[-1]})"
            return text
        return f"list[{len(payload)}]"
    return type(payload).__name__


def find_companies(node, out: list[dict]) -> None:
    if isinstance(node, dict):
        if "companyId" in node:
            out.append(node)
        for value in node.values():
            find_companies(value, out)
    elif isinstance(node, list):
        for value in node:
            find_companies(value, out)


class Result:
    def __init__(self, status, text="", headers=None, error=""):
        self.status = status
        self.text = text
        self.headers = headers or {}
        self.error = error
        try:
            self.json = json.loads(text) if text else None
        except ValueError:
            self.json = None

    def reason(self) -> str:
        if isinstance(self.json, dict):
            for key in ("reason", "message"):
                if self.json.get(key):
                    return scrub(str(self.json[key]))[:120]
        return f"non-JSON body ({len(self.text)} chars)" if self.text else "empty body"


def call(method, path, *, company, jwt="", params=None, body=None) -> Result:
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": f"{ORIGIN}/{company}/services" if company else f"{ORIGIN}/",
        "User-Agent": "Mozilla/5.0 (compatible; mast_probe)",
    }
    if company:  # an empty company means: send no X-CompanyId header at all
        headers["X-CompanyId"] = company
    if jwt:
        headers["jwt"] = f"Bearer {jwt}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return Result(
                resp.status, resp.read().decode("utf-8", "replace"), resp.headers
            )
    except urllib.error.HTTPError as err:
        return Result(err.code, err.read().decode("utf-8", "replace"), err.headers)
    except (urllib.error.URLError, TimeoutError) as err:
        return Result(None, error=scrub(str(err)))


# --------------------------------------------------------------------------- output

CALLS: list[dict] = []
SAVE_DIR: Path | None = None
RAW = False


def record(company: str, name: str, method: str, path: str, res: Result) -> str:
    if res.status is None:
        detail = f"no response ({res.error})"
    elif res.status == 200:
        detail = describe(res.json) if res.json is not None else "non-JSON body"
    else:
        detail = res.reason()
    print(f"  {method:4} {name:34} -> {res.status}  {detail}")
    CALLS.append(
        {
            "company": company,
            "name": name,
            "method": method,
            "path": path,
            "status": res.status,
            "detail": detail,
        }
    )
    if SAVE_DIR and res.json is not None:
        body = res.json if RAW else sanitize(res.json)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
        out = SAVE_DIR / f"{company}_{slug}_{res.status}.json"
        out.write_text(scrub(json.dumps(body, indent=2, ensure_ascii=False)) + "\n")
    return detail


# --------------------------------------------------------------------------- probing


def resume(user_token: str, extra_companies: list[str]):
    """Resume once per X-CompanyId variant and return (jwt, payload) from the first success."""
    variants = list(dict.fromkeys(["", "999999", *extra_companies]))
    chosen = None
    print("Resume header check (does GetUserByToken care about X-CompanyId?):")
    for company in variants:
        label = company or "none"
        res = call(
            "GET", "Users/GetUserByToken", company=company, params={"token": user_token}
        )
        new_token = clean_token(res.headers.get("new-token", "") or "")
        ok = res.status == 200 and isinstance(res.json, dict) and bool(new_token)
        record(
            label, f"resume (X-CompanyId: {label})", "GET", "Users/GetUserByToken", res
        )
        if ok:
            SECRETS.append(new_token)
            returned = (res.json.get("user") or {}).get("token")
            if isinstance(returned, str):
                SECRETS.append(returned)
            if chosen is None:
                chosen = (new_token, res.json)
    if chosen is None:
        sys.exit("Resume failed for every variant: the user token is rejected.")
    return chosen


def report_session(jwt: str, payload: dict) -> list[dict]:
    claims = jwt_claims(jwt)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        when = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        print(f"\nJWT expires {when}")
    print("JWT claim names:", ", ".join(sorted(claims)) or "(unreadable)")
    for key, value in claims.items():
        if "compan" in key.lower():
            print(f"JWT claim {key} = {value}")
    user = payload.get("user")
    print("Payload keys:", ", ".join(payload))
    if isinstance(user, dict):
        print("Payload user keys:", ", ".join(user))
    raw: list[dict] = []
    find_companies(payload, raw)
    merged: dict[str, dict] = {}
    for (
        item
    ) in raw:  # user.companyId and userCompanies[] often describe the same company
        cid = str(item.get("companyId"))
        entry = merged.setdefault(
            cid, {"companyId": cid, "isActiveCompany": None, "names": {}}
        )
        if "isActiveCompany" in item:
            entry["isActiveCompany"] = bool(entry["isActiveCompany"]) or bool(
                item["isActiveCompany"]
            )
        for k, v in item.items():
            if "name" in k.lower() and "compan" in k.lower() and isinstance(v, str):
                entry["names"][k] = v
    found = list(merged.values())
    print(f"Distinct companies in the resume response: {len(found)}")
    for item in found:
        print(
            f"  companyId={item['companyId']} active={item['isActiveCompany']} "
            f"{item['names'] or ''}"
        )
    return found


def probe_company(company: str, jwt: str, card: str, days: int, extras: list[str]):
    print(f"\n=== X-CompanyId {company} ===")

    def get(name, path, **params):
        res = call("GET", path, company=company, jwt=jwt, params=params or None)
        record(company, name, "GET", path, res)
        return res

    # Same order as the web app's water-page load, and as the integration's restore.
    get("ValidateUserAssets", "PersonalAreaAsset/ValidateUserAssets")
    cards_res = get("WaterMeter/GetWaterMeterCards", "WaterMeter/GetWaterMeterCards")
    payer_res = get(
        "PersonalAreaAsset/GetPayerWaterCards", "PersonalAreaAsset/GetPayerWaterCards"
    )

    if not card:
        for res in (cards_res, payer_res):
            if res.status == 200 and isinstance(res.json, list):
                for item in res.json:
                    if isinstance(item, dict) and item.get("waterCardNumber"):
                        card = str(item["waterCardNumber"]).strip()
                        break
            if card:
                break
    if card:
        SECRETS.append(card)
        print(f"  (using card ending ...{card[-3:]})")
        today = date.today()
        body = {
            "waterCardNumber": card,
            "dateFrom": (today - timedelta(days=days)).isoformat(),
            "dateTo": today.isoformat(),
        }
        res = call(
            "POST",
            "WaterMeter/GetWaterMeterCardReading",
            company=company,
            jwt=jwt,
            body=body,
        )
        record(
            company,
            "WaterMeter/GetWaterMeterCardReading",
            "POST",
            "WaterMeter/GetWaterMeterCardReading",
            res,
        )
    else:
        print("  (no card number found, skipping readings)")

    # Unverified: methods and parameters guessed from docs/FINDINGS.md.
    get("(unverified) water/GetWaterDetails", "water/GetWaterDetails")
    get("(unverified) GetWaterPeriods", "PersonalAreaAsset/GetWaterPeriods")
    if card:
        get(
            "(unverified) GetWaterCardConsumptions",
            "PersonalAreaAsset/GetWaterCardConsumptions",
            waterCard=card,
        )
    for spec in extras:
        method, _, path = spec.partition(" ")
        method = method.upper()
        if not path:
            continue
        res = call(method, path, company=company, jwt=jwt)
        record(company, f"(extra) {path}", method, path, res)
    return card


def print_matrix(companies: list[str]) -> None:
    names = []
    for row in CALLS:
        if row["name"] not in names and not row["name"].startswith("resume"):
            names.append(row["name"])
    print("\n=== Status matrix (rows: endpoint, columns: X-CompanyId) ===")
    print(f"{'':44}" + "".join(f"{c:>8}" for c in companies))
    for name in names:
        cells = []
        for company in companies:
            hit = next(
                (r for r in CALLS if r["company"] == company and r["name"] == name),
                None,
            )
            cells.append(f"{(hit['status'] if hit else '-')!s:>8}")
        print(f"{name:44}" + "".join(cells))


def main() -> None:
    global SAVE_DIR, RAW
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--company",
        action="append",
        default=[],
        help="extra X-CompanyId to try (repeatable)",
    )
    parser.add_argument(
        "--control",
        action="append",
        default=[],
        help="a company that is NOT on the account, as a control (e.g. 110)",
    )
    parser.add_argument(
        "--card", default="", help="water card number (default: first one found)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="readings window, default 7 (valid for every supplier)",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help='extra call, e.g. "GET Controller/Action" (repeatable)',
    )
    parser.add_argument(
        "--save",
        metavar="DIR",
        help="write sanitized response bodies (fixture material) to DIR",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="with --save: keep real values (never share these files)",
    )
    args = parser.parse_args()

    user_token = clean_token(os.environ.get("MAST_USER_TOKEN", ""))
    if not user_token:
        user_token = clean_token(getpass.getpass("MAST user token (input hidden): "))
    if not user_token:
        sys.exit("No user token given.")
    SECRETS.append(user_token)

    if args.save:
        SAVE_DIR = Path(args.save)
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
    RAW = args.raw

    jwt, payload = resume(user_token, args.company)
    found = report_session(jwt, payload)

    active = [c["companyId"] for c in found if c["isActiveCompany"]]
    ordered = [*active, *(c["companyId"] for c in found), *args.company, *args.control]
    companies = [c for c in dict.fromkeys(ordered) if c and c not in ("0", "None")]
    if not companies:
        sys.exit("The resume response names no company; pass one with --company.")

    card = args.card
    for company in companies:
        card = probe_company(company, jwt, card, args.days, args.extra) or card

    print_matrix(companies)
    if SAVE_DIR:
        (SAVE_DIR / "calls.json").write_text(json.dumps(CALLS, indent=2) + "\n")
        print(
            f"\nSaved to {SAVE_DIR}/ ({'RAW, do not share' if RAW else 'sanitized'})."
        )
    print(
        "\nTokens and the card number are never printed; bodies are summarized by structure."
    )


if __name__ == "__main__":
    main()

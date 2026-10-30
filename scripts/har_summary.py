#!/usr/bin/env python3
"""Summarize the MAST API calls in a browser HAR file without printing any values.

Use it to see what the real web app calls for a given water corporation (for example whether
the water page of a billing-only corporation has a daily-readings endpoint).

Capture:  DevTools -> Network -> tick "Preserve log" -> sign in, open
          https://www.mast.co.il/<company id>/services and the water page ->
          right-click the list -> "Save all as HAR with content".
Run:      python3 scripts/har_summary.py capture.har [--show KEY ...]

The HAR itself contains tokens and personal data: do not share it. This script prints only
methods, paths, query-parameter and body-key names, status codes, X-CompanyId values and the
structure of JSON bodies, so its output can be pasted into an issue. Scalar values are shown
only for a short allow-list (companyId, isActiveCompany, isValid, reason, anything with
"module" in its name) plus any key you add with --show.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

API_MARKER = "/mast/api/"
INTEREST = ("water", "meter", "reading", "consum", "period", "card", "module", "layout")
ALLOWED_EXACT = {"companyid", "isactivecompany", "isvalid", "issuccess", "reason"}


def describe(payload) -> str:
    if isinstance(payload, dict):
        keys = list(payload)
        return "object keys=" + ",".join(keys[:14]) + (",..." if len(keys) > 14 else "")
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            keys = list(payload[0])
            return f"list[{len(payload)}] of objects keys=" + ",".join(keys[:12])
        return f"list[{len(payload)}]"
    return type(payload).__name__


def allowed_values(payload, extra: set[str], limit: int = 40) -> list[str]:
    """Scalar values of allow-listed keys, from the first items of a list or a dict."""
    out: list[str] = []

    def keep(key: str) -> bool:
        low = key.lower()
        return low in ALLOWED_EXACT or "module" in low or low in extra

    def walk(node) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif (keep(k) or isinstance(v, bool)) and v is not None:
                    out.append(f"{k}={str(v)[:60]}")
        elif isinstance(node, list):
            for item in node[:limit]:
                walk(item)

    walk(payload)
    return out


def header(headers: list[dict], name: str) -> str | None:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def body_json(content: dict):
    text = content.get("text")
    if not text:
        return None, "(body not saved: export the HAR with content)"
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", "replace")
        except ValueError:
            return None, "(undecodable body)"
    try:
        return json.loads(text), ""
    except ValueError:
        return None, f"non-JSON {content.get('mimeType', '')} ({len(text)} chars)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("har", type=Path)
    parser.add_argument(
        "--show",
        action="append",
        default=[],
        help="also show values of this key (repeatable)",
    )
    args = parser.parse_args()
    extra = {k.lower() for k in args.show}

    try:
        entries = json.loads(args.har.read_text(encoding="utf-8"))["log"]["entries"]
    except (OSError, ValueError, KeyError) as err:
        sys.exit(f"Cannot read {args.har} as a HAR file: {err}")

    rows = []
    companies: dict[str, int] = {}
    for entry in entries:
        req, resp = entry.get("request", {}), entry.get("response", {})
        url = urlparse(req.get("url", ""))
        if API_MARKER not in url.path or req.get("method") == "OPTIONS":
            continue
        path = url.path.split(API_MARKER, 1)[1]
        names = ",".join(k for k, _ in parse_qsl(url.query, keep_blank_values=True))
        company = header(req.get("headers", []), "X-CompanyId")
        if company:
            companies[company] = companies.get(company, 0) + 1
        post = (req.get("postData") or {}).get("text")
        req_body = ""
        if post:
            try:
                req_body = " body " + describe(json.loads(post))
            except ValueError:
                req_body = " body (non-JSON)"
        payload, note = body_json(resp.get("content", {}))
        status = resp.get("status")
        if payload is not None:
            detail = describe(payload)
            if status and status >= 400 and isinstance(payload, dict):
                detail = (
                    f"{payload.get('reason') or payload.get('message') or ''}"[:120]
                    or detail
                )
            vals = allowed_values(payload, extra)
        else:
            detail, vals = note, []
        flags = []
        if header(req.get("headers", []), "jwt"):
            flags.append("jwt")
        if header(resp.get("headers", []), "new-token"):
            flags.append("new-token")
        rows.append(
            (
                req.get("method"),
                path,
                names,
                company,
                status,
                detail,
                req_body,
                flags,
                vals,
            )
        )

    if not rows:
        sys.exit(
            f"No calls to {API_MARKER} found. Was the HAR captured with Preserve log on?"
        )

    print(
        f"{len(rows)} API calls. X-CompanyId values seen: "
        + ", ".join(f"{c} (x{n})" for c, n in companies.items())
        + "\n"
    )
    print("=== In order ===")
    for i, (
        method,
        path,
        names,
        company,
        status,
        detail,
        req_body,
        flags,
        vals,
    ) in enumerate(rows, 1):
        query = f"?{names}=..." if names else ""
        print(
            f"{i:3}. {method:4} {path}{query} [{company or '-'}] -> {status} {detail}{req_body}"
            + (f"  <{','.join(flags)}>" if flags else "")
        )
        for v in vals[:12]:
            print(f"       {v}")

    print("\n=== Water-related calls (unique) ===")
    seen: set[tuple] = set()
    for method, path, names, _c, status, detail, _rb, _f, _v in rows:
        key = (method, path)
        if key in seen or not any(word in path.lower() for word in INTEREST):
            continue
        seen.add(key)
        query = f"?{names}=..." if names else ""
        print(f"  {method:4} {path}{query} -> {status}  {detail}")
    print(
        "\nValues are hidden except the allow-list; the HAR file itself is still sensitive."
    )


if __name__ == "__main__":
    main()

# Findings: how the MAST water API behaves

Investigated 2026-10-13 against Mei Raanana's tenant (`X-CompanyId: 110`) using a normal account, a
browser, and `curl` with that account's own token. Everything here is what was observed, not documentation:
MAST has no public API docs. Personal values (card number, address, phone, ID, tokens) are deliberately left out.

## Summary

- There is an authenticated JSON API behind the web app at `https://api.mast.co.il/mast/api/...`.
- It returns **one cumulative meter reading per day** (m³), so history and daily usage can be read directly.
- Authentication is a **JWT valid for 48 hours**, obtained by an **SMS code login that is protected by a
  Google reCAPTCHA token**. No refresh mechanism was found.
- Therefore a Home Assistant integration cannot log in unattended. It has to receive a token that the user
  obtained by logging in in a browser. That is the design of this project.

## Endpoints seen

Base: `https://api.mast.co.il/mast/api`. Common headers: `jwt: Bearer <token>`, `X-CompanyId: 110`,
`Content-Type: application/json`, and browser-like `Origin`/`Referer` of `https://www.mast.co.il`.

| Method and path | Purpose | Notes |
|---|---|---|
| `POST /WaterMeter/GetWaterMeterCardReading` | Daily readings | Body `{"waterCardNumber","dateFrom","dateTo"}` (dates `YYYY-MM-DD`). Returns `[{"readingDate":"2026-10-01T00:00:00","reading":380.723}, ...]`. |
| `GET /WaterMeter/GetWaterMeterCards` | List the account's cards | Returns `[{"waterCardNumber","address"}]`. Lets the integration offer a card picker. |
| `POST /users/LogIn` | Step 1 of login: send the SMS | Body has `phone`, `isVocalMessage`, `originLogin`, `recaptchaCode`. Response: `{"securedPhone":"054-****xxx","isSuccess":true,"message":null}`. |
| `POST .../ValidateSmsCodeLogIn` | Step 2 of login: check the code | Response contains a `user` object including a `token` (and one per company in `userCompanies`). The exact request body and whether it needs a captcha token were **not captured**. |

Seen in the web app's traffic but not investigated: `GetWaterPeriods`, `GetWaterCardConsumptions?waterCard=`,
`GetPayerWaterCards`, `GetLastStubsDataByPayerId`, `GetDocumentsByPayerId` (probably billing-period data
and documents), plus company/layout/user-settings calls. Their controller paths were not found
(`GetWaterCardConsumptions` is not under `/WaterMeter/`).

## Data semantics

- Values are cumulative meter totals in m³ with three decimals; daily usage is the difference between days.
- Timestamps are naive local times at midnight (`T00:00:00`). Whether a reading is the value at the start or
  the end of that date was **not determined**. The integration treats it as the value at midnight starting that date.
- A request on 13 Oct for 1 to 13 Oct returned readings up to 12 Oct, so the newest reading is about a day old.
- Not determined: how far back history goes, and the largest date range one request accepts. The integration
  asks in 90-day chunks and skips chunks that fail.

## Authentication model

- **Token:** a standard JWT (HS256). Claims seen: a user identifier (a 9-digit number, probably the account holder's
  ID number), a mobile phone number, `nbf`, `iat`, `exp`, `iss` and `aud` (both `https://www.mast.co.il/`). Claims are only
  base64-encoded, so anyone who sees the token can read them.
- **Lifetime:** `exp - iat` was exactly 48 hours.
- **Sent as** the request header `jwt: Bearer <token>`, not `Authorization`.
- **Logout does not revoke it.** After logging out in the browser the same token still returned data. The
  server evidently validates the signature and expiry only. Consequences: a leaked token is usable until it
  expires, and there is no server-side "log out everywhere".
- **No renewal found.**
  - Calls to the data endpoint return no new token header (no `jwt`, `authorization` or `set-cookie`
    in the response headers), so the token is not renewed by using it.
  - The login response contained only the `token` (plus per-company copies); no refresh token field was seen.
  - A small probe of guessed refresh endpoints (`RefreshToken`, `Refresh`, `RenewToken`, `ExtendToken`
    under a few controllers) returned 404. This is a limited probe, not proof.
- **Login** is two steps: `users/LogIn` sends an SMS, then an SMS-code validation call returns the token.
  The first step includes a `recaptchaCode`.

## The reCAPTCHA

The SMS-send request carried a `recaptchaCode` of 2,340 characters starting `0cAF`. That is a token produced by
Google's script in the user's browser (consistent with reCAPTCHA Enterprise). No visible challenge was shown,
so it is the invisible, score-based kind. A server-side process like Home Assistant cannot produce a valid one.

**Decision:** this project does not forge, solve, replay or bypass it, and does not test whether the server
enforces it. Unattended login is therefore out of scope. The supported route is: the user logs in on the real
site (the browser makes the token), and the resulting session token is handed to Home Assistant.

## Behaviour of errors

- A wrong or placeholder `waterCardNumber` gives **HTTP 500** with a generic Hebrew message
  (`{"message":"שגיאה בהצגת הנתונים"}`, "error displaying the data"), not a 4xx. The integration therefore
  reports a 500 as "check the card number".
- An unknown controller or action gives a 404 with a Web API style message.
- What an expired or invalid token returns was **not observed**. The integration treats 401 and 403 as
  "token rejected" and additionally checks the `exp` claim itself before every poll.

## Open questions

- Does `ValidateSmsCodeLogIn` also require a captcha token? If it does not, a "type the SMS code into Home
  Assistant" flow would be possible (the SMS would still have to be triggered in a browser).
- Is the token bound to an IP address or browser? It worked from the machine that logged in; use from a
  different network (the Home Assistant host) was not separately tested.
- Real history depth and maximum range per request.
- Whether the reading is start-of-day or end-of-day.
- Whether Mei Raanana or MAST would offer an official API key or a longer-lived token for home automation.
  Asking them is the cleanest long-term fix.

## How this was found (to repeat it for another tenant)

1. Log in in the browser with DevTools → Network open and **Persist Logs** on.
2. Find the data call (`GetWaterMeterCardReading`), right-click → Copy as cURL, and replay it with the token
   in an environment variable rather than pasted into the command.
3. Decode only the timestamps of the JWT to learn its lifetime.
4. Compare response headers over several calls for a renewed token.
5. For another water corporation using MAST, change `X-CompanyId` to its company id (visible in the request
   headers and in URLs such as `https://www.mast.co.il/<id>/services`).

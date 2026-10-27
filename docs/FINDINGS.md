# How the MAST water API behaves

What this integration relies on, written down so it can be checked or repeated. It was observed against
Mei Raanana's tenant (`X-CompanyId: 110`) with a normal resident account, a browser and the account's own
token. MAST has no public API documentation, so everything here is observed behaviour and can change.
Personal values (card number, address, phone, ID, tokens) are deliberately left out.

## Summary

- An authenticated JSON API sits behind the web app at `https://api.mast.co.il/mast/api/`.
- It returns **one cumulative meter reading per day** (m³), so history and daily usage can be read directly.
- Authentication is a **JWT valid for 48 hours**, obtained by an **SMS-code login in which both steps carry a
  Google reCAPTCHA v3 token**. A server-side client cannot log in unattended; the user logs in in a browser
  and the token is handed to Home Assistant.
- A few hours after login MAST drops the **server-side state behind the `WaterMeter` endpoints**. They then
  answer 401 `Expired|NotValid` although the token is still valid. Two calls restore it (see "Session restore").
- No token refresh exists that a client can use. The web app's `new-token` header mechanism has never been seen
  in a response.

## Endpoints

Base `https://api.mast.co.il/mast/api/`. Headers: `jwt: Bearer <token>` (not `Authorization`),
`X-CompanyId: 110`, `Content-Type: application/json`, browser-like `Origin` and `Referer` of
`https://www.mast.co.il`.

| Method and path | Purpose | Notes |
|---|---|---|
| `POST WaterMeter/GetWaterMeterCardReading` | Daily readings | Body `{"waterCardNumber","dateFrom","dateTo"}` (`YYYY-MM-DD`). Returns `[{"readingDate":"2026-10-01T00:00:00","reading":380.723}, ...]`. |
| `GET WaterMeter/GetWaterMeterCards` | The account's water cards | Returns `[{"waterCardNumber","address"}]`. |
| `GET PersonalAreaAsset/ValidateUserAssets` | Route guard the web app runs before the water page | Returns `{"isValid": bool, "message": ...}`. |
| `POST users/LogIn` | Login step 1: send the SMS | Body has `phone`, `isVocalMessage`, `originLogin`, `recaptchaCode`. |
| `POST users/ValidateSmsCodeLogIn` | Login step 2: check the code | Also carries a `recaptchaCode`. |

Other calls the website makes when it loads (all `200` with a valid token, not used here): `GetCities`,
`GetCompanyModules`, `GetLayoutByCompanyId`, `water/GetWaterDetails`, `forms/GetUserOpened`, `forms/GetUserSaved`,
`forms/GetFormsByCompanyId`, `stubs/GetLastStubsDataByPayerId`, `Documents/GetDocumentsByPayerId`.

`PersonalAreaAsset/GetPayerWaterCards`, `GetWaterPeriods` and `GetWaterCardConsumptions?waterCard=` exist for
resident sessions (probably billing-period data, not investigated). The `ExternalUsersPersonalArea/*` family
belongs to a different short session, opened from a link in a bill (`External-User-Authorization` header, 30-minute
client timer), and does not accept a resident token.

## Data semantics

- Values are cumulative totals in m³ with three decimals. Daily usage is the difference between consecutive days.
  The web app's daily consumption page does exactly this (reading minus the previous reading, shown on the later
  date), and the integration attributes usage to days the same way.
- Timestamps are naive local times at midnight (`T00:00:00`). Whether a reading is the meter value at the start
  or the end of that date is not documented; the integration treats it as the value at midnight starting that date.
- The newest reading is about a day old.
- **Range limit:** at most 30 days between `dateFrom` and `dateTo`, or 7 days when the company's meter supplier is
  `Arad` or `AradHarmony`. The date picker goes back about 730 days.
- **Status codes as the web app reads them:** `404` = no data in the range, `400` = invalid dates, other = error.
  A wrong card number gives HTTP `500` with a generic Hebrew message (`שגיאה בהצגת הנתונים`, "error displaying the
  data").
- The integration therefore requests 30-day windows (up to 730 days back on the first run), switches to 7-day
  windows when MAST answers 400, and treats 404 as an empty range.

## Authentication

- **Token:** a standard JWT (HS256) with a user identifier, a mobile phone number, `nbf`, `iat`, `exp`, `iss` and
  `aud`. The claims are only base64-encoded, so anyone who sees the token can read them.
- **Lifetime:** `exp - iat` is exactly 48 hours.
- **Logout does not revoke it:** after logging out in the browser the same token still returned data, so the
  server checks signature and expiry (and the session state below), not a revocation list.
- **Responses:** an expired token gives HTTP 401 with a JSON body `{"reason": "expired"}`, which the web app turns
  into "log in again". A 401 with `Expired|NotValid` is the lapsed session state below, not an expired token.
  Cloudflare sits in front of the API (`server: cloudflare`); a 403 with an HTML body is a block by it, not a MAST
  error.
- **Renewal:** the web app's HTTP interceptor replaces its token whenever a response carries a `new-token`
  header. In every response observed after login, on the data call and on all the other calls, the header was
  absent, so no renewal is known to happen. The integration adopts such a header if one ever arrives.
- **Login:** `users/LogIn` sends an SMS and `users/ValidateSmsCodeLogIn` checks the code. Both requests include a
  `recaptchaCode`, a token that Google's script produces in the browser (the invisible, score-based kind). It
  cannot be produced by Home Assistant.

**Decision:** this project does not forge, solve, replay or bypass the reCAPTCHA, does not test whether the server
enforces it, and does not try to read the SMS. Unattended login is out of scope. The user logs in on the real site,
and the resulting token is pushed to Home Assistant (by hand or with the userscript).

## Session restore

A few hours after login the `WaterMeter` endpoints start answering 401 `Expired|NotValid` while the token itself
is unexpired and every other call (user data, forms, documents, `water/GetWaterDetails`) still returns 200. Opening
the water page on the website makes it work again. Calling the page's own two calls, **in this order**, does the same:

| Call made while refused | Its answer | Then the readings call answers |
|---|---|---|
| none | | 401 `Expired|NotValid` |
| `PersonalAreaAsset/ValidateUserAssets` | 200 | 500, the generic error (as for an unknown card) |
| `WaterMeter/GetWaterMeterCards` | 200 | 200, readings |

Neither call alone is enough. The other page-load calls have no effect. This is the order of the website's own
water-page load (`ValidateUserAssets`, `GetWaterMeterCards`, `GetWaterMeterCardReading`). The integration makes
the two calls whenever an unexpired token is refused and retries at once. How long the restored state lasts was
not measured; the first refusal came about six hours after the token was issued in the cases seen.

## The Android app

`com.mgar.mast.mastapp` is a Trusted Web Activity (generated by `twa_generator`). It contains no API client, no
token handling, no PIN or biometric login and no attestation code of its own; it opens
`https://www.mast.co.il/` in Chrome. Login, reCAPTCHA and session are the website's, so the app offers no renewal
that the website lacks.

## Web app (static reading of the public bundles)

An Angular single-page app. Every API call goes through one service that prefixes the base URL, adds
`jwt: Bearer <token>` (from localStorage) and `X-CompanyId`, and handles responses as described above. The water
page calls `GetWaterMeterCards` on load (and preselects the card if there is only one), then
`GetWaterMeterCardReading`. There is no keep-alive, ping or refresh call anywhere in the bundles. The build flag
`isApp` switches some behaviour for the app wrapper.

## Open questions

- Does MAST ever send `new-token`, for example close to expiry? The integration would store it and log
  `MAST renewed the session token`.
- How long does the restored session state last, and what exactly drops it?
- Is a reading the meter value at the start or end of its date?
- Would Mei Raanana or MAST offer an API key or a longer-lived token for home automation? Asking them is the
  cleanest long-term fix.

## Reproducing this for another water corporation

1. Log in in the browser with DevTools → Network open and Persist Logs on.
2. Find `GetWaterMeterCardReading`, copy it as cURL and replay it with the token in an environment variable
   instead of pasted into the command.
3. Decode only the timestamps of the JWT to learn its lifetime.
4. Change `X-CompanyId` to the other company's id (visible in request headers and in URLs such as
   `https://www.mast.co.il/<id>/services`).

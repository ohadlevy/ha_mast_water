# How the MAST water API behaves

What this integration relies on, written down so it can be checked or repeated. It was observed against
Mei Raanana's tenant (`X-CompanyId: 110`) with a normal resident account, a browser and the account's own
token. MAST has no public API documentation, so everything here is observed behaviour and can change.
Personal values (card number, address, phone, ID, tokens) are deliberately left out.

## Summary

- An authenticated JSON API sits behind the web app at `https://api.mast.co.il/mast/api/`.
- It returns **one cumulative meter reading per day** (m³), so history and daily usage can be read directly.
- Authentication is a **JWT valid for 48 hours**, obtained by an **SMS-code login in which both steps carry a
  Google reCAPTCHA v3 token**. The initial credentials come from the user's browser. The integration can then
  obtain or renew the JWT using the separate remembered-login `user.token` (see "Authentication").
- A few hours after login MAST drops the **server-side state behind the `WaterMeter` endpoints**. They then
  answer 401 `Expired|NotValid` although the token is still valid. Two calls restore it (see "Session restore").
- No dedicated refresh-token endpoint has been found. The app startup code calls `Users/GetUserByToken`
  using `user.token` returned by login. Repeated exchanges without a JWT, OTP, or reCAPTCHA returned a new JWT
  in `new-token` with a later expiry and working account access. The integration renews before expiry and
  persists both credentials. The opaque credential's lifetime and post-expiry behavior remain unverified.

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
| `GET Users/GetUserByToken?token=…` | App startup session resume / JWT renewal | Uses login response `user.token`, omits the old JWT. Returns `isSuccess`, `user`, and a `new-token` response header. |

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

- **JWT:** a standard JWT (HS256) with a user identifier, a mobile phone number, `nbf`, `iat`, `exp`, `iss` and
  `aud`. The claims are only base64-encoded, so anyone who sees the token can read them.
- **Lifetime:** `exp - iat` is exactly 48 hours.
- **Logout does not revoke it:** after logging out in the browser the same token still returned data, so the
  server checks signature and expiry (and the session state below), not a revocation list.

The website uses three values that serve different roles:

| Value | Origin and use |
|---|---|
| `jwtToken` | API JWT, sent as `jwt: Bearer …`; the HTTP interceptor replaces it when a response has `new-token`. |
| `user.token` / `userToken` | Returned in the successful login's user object and sent to `Users/GetUserByToken`. In normal web mode, read it from `JSON.parse(localStorage.getItem('user')).token`. Its server lifetime is unknown. |
| `token.val` / `loginDto.token` | Client-generated UUID used during OTP verification. The separate localStorage `token` wrapper has a two-day client expiry. It may equal `user.token`, but that relationship is unverified; use the server-returned `user.token`. |

The app startup code checks its stored `userToken`, calls `Users/GetUserByToken`, then stores the returned user
and navigates to the active company's services. This path has no OTP or reCAPTCHA request. In the captured web
build (`isApp: false`), the separate `userToken` storage entry is usually absent, but the `user` object still
contains its `token` field. After `user.expirationDate`, the web client's getter clears its local storage.

- **Responses:** an expired token gives HTTP 401 with a JSON body `{"reason": "expired"}`, which the web app turns
  into "log in again". A 401 with `Expired|NotValid` is the lapsed session state below, not an expired token.
  Cloudflare sits in front of the API (`server: cloudflare`); a 403 with an HTML body is a block by it, not a MAST
  error.
- **Renewal:** the web app's HTTP interceptor adopts `new-token`. Previously observed ordinary data responses
  lacked this header, but `Users/GetUserByToken` was verified to return it. A query credential from
  `user.token` is sufficient; no existing JWT, cookies, OTP or reCAPTCHA were supplied in the successful
  exchanges. The returned identity claims matched the original JWT and the expiry increased on each exchange.
  The previously captured `user.token` still worked after one observed logout and new OTP login, and the
  returned opaque value was unchanged. MAST may still revoke it in other circumstances.
- **Integration behavior:** renew during the final 12 hours of JWT validity, or after an auth failure that
  session restoration did not fix. Persist the JWT and opaque-token rotation together. Temporary renewal
  outages keep a valid JWT in use or retry if it has expired. A rejected renewal credential eventually
  requires user sign-in. Lifetime beyond the tested short interval remains unknown.
- **Login:** `users/LogIn` sends an SMS and `users/ValidateSmsCodeLogIn` checks the code. Both requests include a
  `recaptchaCode`, a token that Google's script produces in the browser (the invisible, score-based kind). It
  cannot be produced by Home Assistant.

**Decision:** this project does not forge, solve, replay or bypass the reCAPTCHA, does not test whether the server
enforces it, and does not try to read the SMS. Fresh login is performed by the user on the real site. Using the
site's own token-based session-resume flow uses a credential obtained from a successful login and does not
involve automating an OTP challenge.

### Renewal verification status (2026-10-29)

- Repeated manual `GetUserByToken` exchanges returned working JWTs with later expiries and no OTP. On
  2026-10-28 at 20:09 UTC, the original session token still resumed about 23 hours after issuance; the
  returned JWT expired on 2026-10-30 at 20:09 UTC.
- At 2026-10-29 09:03 UTC, a manual exchange with the same session token succeeded about 36 hours after
  issuance, inside the original JWT's final 12 hours. The new JWT expired on 2026-10-31 at 09:03 UTC,
  later than the previously saved JWT's 2026-10-30 14:47 UTC expiry. The opaque token was unchanged.
  The original JWT had not yet expired.
- The same `user.token` worked after one browser logout followed by a new OTP login. This is one observation,
  not evidence that logout never revokes it.
- Automatic renewal in an unattended Home Assistant installation and renewal after the original JWT expires
  have not yet been observed. The tests cover both paths with a mocked MAST API.
- The server lifetime of `user.token`, and whether repeated renewal extends it, are unknown. Keep the
  12-hour `TOKEN_RENEWAL_WINDOW` until unattended renewal and a post-expiry exchange can be measured.

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
`https://www.mast.co.il/` in Chrome. Login, reCAPTCHA and session are the website's. This does not establish that
all legacy app code in the web bundles is unused by the API: a separate `isApp` path stores `user.token` and
uses `GetUserByToken` at startup. The captured production web build has `isApp: false`.

## Web app (static reading of the public bundles)

An Angular single-page app. Every API call goes through one service that prefixes the base URL, adds
`jwt: Bearer <token>` (from localStorage) and `X-CompanyId`, and handles responses as described above. The water
page calls `GetWaterMeterCards` on load (and preselects the card if there is only one), then
`GetWaterMeterCardReading`. No dedicated refresh-token call was found. A follow-up search of all chunks listed
by the captured runtime manifest found the session-resume call in the lazy-loaded app startup bundle that the
initial page capture did not include.

## Open questions

- How long does the remembered-login `user.token` remain usable? Does successful renewal extend its own
  server lifetime? Does it continue working after the original JWT expires?
- The opaque token did not rotate in the observed exchanges. Under what conditions does it rotate?
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

# Findings: how the MAST water API behaves

Investigated 2026-10-13 against Mei Raanana's tenant (`X-CompanyId: 110`) using a normal account, a
browser, and `curl` with that account's own token. Everything here is what was observed, not documentation:
MAST has no public API docs. Personal values (card number, address, phone, ID, tokens) are deliberately left out.

## Summary

- There is an authenticated JSON API behind the web app at `https://api.mast.co.il/mast/api/...`.
- It returns **one cumulative meter reading per day** (m³), so history and daily usage can be read directly.
- Authentication is a **JWT valid for 48 hours**, obtained by an **SMS code login where both steps carry a
  Google reCAPTCHA v3 token**.
- The web app's code shows a **renewal mechanism: the server can return a fresh token in a `new-token`
  response header**, which the app swaps in on every response. Whether the server actually sends it, and
  when, has to be confirmed with a live test (see "Open questions"). The integration supports it.
- A Home Assistant integration still cannot log in unattended. It has to receive a token that the user
  obtained by logging in in a browser. That is the design of this project.

## Endpoints seen

Base: `https://api.mast.co.il/mast/api`. Common headers: `jwt: Bearer <token>`, `X-CompanyId: 110`,
`Content-Type: application/json`, and browser-like `Origin`/`Referer` of `https://www.mast.co.il`.

| Method and path | Purpose | Notes |
|---|---|---|
| `POST /WaterMeter/GetWaterMeterCardReading` | Daily readings | Body `{"waterCardNumber","dateFrom","dateTo"}` (dates `YYYY-MM-DD`). Returns `[{"readingDate":"2026-10-01T00:00:00","reading":380.723}, ...]`. |
| `GET /WaterMeter/GetWaterMeterCards` | List the account's cards | Returns `[{"waterCardNumber","address"}]`. Lets the integration offer a card picker. |
| `POST /users/LogIn` | Step 1 of login: send the SMS | Body has `phone`, `isVocalMessage`, `originLogin`, `recaptchaCode`. Response: `{"securedPhone":"054-****xxx","isSuccess":true,"message":null}`. |
| `POST /users/ValidateSmsCodeLogIn` | Step 2 of login: check the code | Request carries a `recaptchaCode` too (from the app's code). Response contains a `user` object including a `token` field and `expirationDate`; the JWT the app then uses arrives via the `new-token` header. |

Related endpoints found in the web app's code (chunk files), by session type:

| Resident area (SMS login, `jwt` header) | Same thing for the bill-link session |
|---|---|
| `GET /PersonalAreaAsset/GetPayerWaterCards` | `GET /ExternalUsersPersonalArea/GetPayerWaterCards` |
| `GET /PersonalAreaAsset/GetWaterPeriods` | `GET /ExternalUsersPersonalArea/GetWaterPeriods` |
| `GET /PersonalAreaAsset/GetWaterCardConsumptions?waterCard=<card>` | `GET /ExternalUsersPersonalArea/GetWaterCardConsumptions?waterCard=<card>` |

These are probably billing-period data (not investigated). `GET /water/GetWaterDetails` also exists.
The `ExternalUsersPersonalArea` family belongs to a different, short session: the "external user" session you
get by opening a link from a bill or message (`Stubs/LoginForBillByGuidId`, `LoginForBillByMessage`). It authenticates
with an `External-User-Authorization` header (the server hands it out in a response header of the same name; the
app expires it client-side after 30 minutes), not `jwt`. Calling it with a resident `jwt` returned a Hebrew error
body meaning "during login an invalid authority identifier was received". That flow was not investigated further.

Seen in the web app's traffic but not investigated: `GetWaterPeriods`, `GetWaterCardConsumptions?waterCard=`,
`GetPayerWaterCards`, `GetLastStubsDataByPayerId`, `GetDocumentsByPayerId` (probably billing-period data
and documents), plus company/layout/user-settings calls. Their controllers are listed above.

## Data semantics

- Values are cumulative meter totals in m³ with three decimals; daily usage is the difference between days.
- **The web app does exactly that.** The "daily consumption data" page (lazy chunk with the `water-meter-reader`
  component) sorts readings by date and shows `reading - previous reading` on the later date's row, plus min,
  max, sum and average. The integration attributes usage to days the same way.
- Timestamps are naive local times at midnight (`T00:00:00`). Whether a reading is the value at the start or
  the end of that date was **not determined**. The integration treats it as the value at midnight starting that date.
- A request on 13 Oct for 1 to 13 Oct returned readings up to 12 Oct, so the newest reading is about a day old.
- **Range limit.** The page only allows a span of at most 30 days between `dateFrom` and `dateTo`, and at most
  **7 days** when the company's meter supplier is `Arad` or `AradHarmony` (supplier ids 6 and 7 in the app's
  supplier enum, set per company). Its default window is the last 30 (or 7) days inclusive. The date picker goes
  back about 730 days. A 30-day request ending today is what worked in testing. The server-side limit itself was
  not probed.
- **Status codes as the page reads them:** 404 means "no data" (empty result), 400 means "dates not valid",
  anything else is a generic API error. A bad card number was seen as HTTP 500.
- The integration therefore requests 30-day windows (up to 730 days back on first run), switches to 7-day
  windows if MAST answers 400, and treats 404 as an empty range.

## Authentication model

- **Token:** a standard JWT (HS256). Claims seen: a user identifier (a 9-digit number, probably the account holder's
  ID number), a mobile phone number, `nbf`, `iat`, `exp`, `iss` and `aud` (both `https://www.mast.co.il/`). Claims are only
  base64-encoded, so anyone who sees the token can read them.
- **Lifetime:** `exp - iat` was exactly 48 hours.
- **Sent as** the request header `jwt: Bearer <token>`, not `Authorization`.
- **Logout does not revoke it.** After logging out in the browser the same token still returned data. The
  server evidently validates the signature and expiry only. Consequences: a leaked token is usable until it
  expires, and there is no server-side "log out everywhere".
- **Renewal (from the web app's code).** The app's HTTP interceptor runs on every response: if the response has
  a `new-token` header, it replaces the stored token with it. This is the *only* place the app assigns its
  `jwt` token, so the login response itself also delivers the token through that header, and later responses
  can replace it. This is a sliding-session design. It is not known whether the server sends `new-token` on
  every call, only near expiry, or only at login.
- **An early test missed it.** A first check of response headers filtered for `jwt`, `authorization`,
  `set-cookie` and `x-*token`, which cannot match a header named `new-token`, so "no renewal header" was a
  wrong conclusion. Probes of guessed refresh endpoints returned 404, which fits the app having no separate
  refresh call.
- **Expired token response.** The app treats **HTTP 401 with a JSON body whose `reason` is `"expired"`** as
  an expired session (it shows "connection time ended, log in again" and logs out).
- **Client-side timers.** The app logs itself out at the `expirationDate` in the login response's `user`
  object, and keeps a separate client-side token record with a 2-day expiry. Separately, "external users"
  (a different flow that identifies by payer number) use an `External-User-Authorization` header and a
  30-minute timer. That flow was not investigated.
- **Login** is two steps: `users/LogIn` sends an SMS, then an SMS-code validation call returns the token.
  Both steps include a `recaptchaCode`: the app's code gets a reCAPTCHA v3 token
  (`executeImportantAction`) before calling `users/LogIn` and again before `users/ValidateSmsCodeLogIn`.

## The reCAPTCHA

Both login requests carry a `recaptchaCode`. The SMS-send request's was of 2,340 characters starting `0cAF`. That is a token produced by
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

- **Does the server send `new-token`, and when?** If it sends a fresh 48-hour token on ordinary data calls, a
  poller that stores it keeps the session alive indefinitely and no re-login is needed. Test: log in, call the
  data endpoint with `curl -D` and look for a `new-token` response header (decode only `iat`/`exp`), then repeat
  after some hours. There may be a cap on total session age.
- Answered: `ValidateSmsCodeLogIn` also carries a reCAPTCHA token, so a "type the SMS code into Home Assistant"
  flow is not possible.
- Is the token bound to an IP address or browser? It worked from the machine that logged in; use from a
  different network (the Home Assistant host) was not separately tested.
- The real server-side limits (the page limits are known, see above).
- Whether the reading is the meter value at the start or end of its date (the web app shows the difference to the
  previous reading on the later date, and the integration matches that).
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

## Web app source (static analysis, 2026-10-15)

The site is an Angular single-page app (webpack bundles `runtime`, `polyfills`, `scripts`, `main`, plus 40-odd
lazy chunks, no source maps checked). Public bundles were downloaded from the browser and read; nothing
was executed against the API for this. Facts taken from `main`:

- API base `https://api.mast.co.il/mast/`; every call goes through one service that prefixes it.
- Request interceptor adds `jwt: Bearer <token>` (from localStorage `jwtToken`) and `X-CompanyId`.
- Response handling as described above (`new-token`, 401 `reason: "expired"`).
- Other endpoints named in `main`: `api/PersonalAreaAsset/ValidateUserAssets`,
  `.../ValidateUserInPriority`, `.../GetAssetTransactionHistory?propertyNumber=`,
  `.../GeneratePendingBalancePaymentCode`, `api/users/Registration`, `ResendRegistrationCode`,
  `ValidateCodeRegistration`. The water pages (`GetWaterMeterCards` etc.) live in lazy chunks, which were not
  downloaded.
- The build has `isApp: false`; an `isApp` flag switches some behaviour and stores an extra `userToken` in
  localStorage, so the mobile app is probably the same Angular code in a wrapper. The mobile app itself was not
  examined. `fingerprint` in the bundle is only Sentry error grouping, not biometric login.

Lazy chunks (40 files, read on 2026-10-15): the water consumption page is in chunk `36`, the shared water
endpoints in `common`. The page calls `api/WaterMeter/GetWaterMeterCards` on load (and preselects the card if there is
only one), then `api/WaterMeter/GetWaterMeterCardReading` with `{waterCardNumber, dateFrom, dateTo}`, which is
exactly what this integration does. The company's configuration supplies `waterMeterReaderSupplier`.
Mistake corrected during this work: an early list of endpoint paths taken from a page-fetch summarizer had the
wrong controller for the water-period calls; always check paths against the downloaded files.

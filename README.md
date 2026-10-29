# MAST Water for Home Assistant

Imports your daily water meter readings from the **MAST** resident portal (used by Mei Raanana and other
Israeli water corporations) into Home Assistant, including a backfill of your history, so the data works in
the **Energy dashboard** and in long-term statistics.

> **Unofficial.** It calls the same API the MAST web app uses, with your own session token, for your own data.
> MAST can change or block that at any time. Not affiliated with MAST or Mei Raanana. What is known about the
> API and why the integration works the way it does is in [docs/FINDINGS.md](docs/FINDINGS.md).

## How it works

MAST has no documented public API. Initial login uses an SMS code plus a Google reCAPTCHA.
**You log in yourself, in your browser.** MAST uses a JWT for water API requests and a separate
remembered-login token (`user.token`) that can obtain new JWTs. The JWT lasts **48 hours**.

Supply `user.token` during setup. Home Assistant obtains the first JWT from
it and exchanges it again during the final 12 hours of each JWT's validity. This flow was verified repeatedly
against MAST without another OTP. The renewal credential's server lifetime is still unknown; if MAST
rejects it, the integration asks you to sign in again. See [the API findings](docs/FINDINGS.md#authentication).

If a session cannot be renewed, the next time Home Assistant has a valid token it fetches every day since the
last import and fills in the history with the correct dates.

A few hours after login MAST drops the server-side setup behind its meter endpoints, even though the token is
still valid. When that happens the integration makes the same two calls the website makes before it shows the
water page, and retries.

## What you get

| Item | Meaning |
|---|---|
| Statistic `mast_water:meter_<card>` ("MAST water meter", m³) | The full history. **Use this in the Energy dashboard.** |
| Sensor *Meter reading* | Latest cumulative reading (m³) |
| Sensor *Latest daily usage* | Difference between the two most recent daily readings (m³) |
| Sensor *Access token expires* (diagnostic) | When the current JWT expires. Attributes: `issued`, `renewals_since_start`, `rejections_in_a_row`, `last_rejection` |

The sensors deliberately have no state class, so they do not create a second series that would double count in
the Energy dashboard.

## Install

1. Copy `custom_components/mast_water` into your Home Assistant `config/custom_components/` (or add this
   repository to HACS as a custom repository and install it there).
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → MAST Water.**

## Setup

### Connect your MAST session (manual, one time)

1. Log in at <https://www.mast.co.il> and open your water consumption page.
2. Open browser DevTools → **Console** and run
   `copy(JSON.parse(localStorage.getItem('user')||'{}').token)`. Chrome and Firefox may ask you to type
   `allow pasting` before pasting the snippet. Paste the copied value into **Session token** in the
   integration, then clear your clipboard. Home Assistant obtains a JWT and lists your water cards.
3. Choose your water card if the account has more than one.

The first run imports up to two years of history in 30-day requests (the most the MAST web app allows at once;
it falls back to 7 days for water corporations that only allow that). Ranges with no data are skipped.
After upgrading to the date-range fix, the first poll rereads history from the earliest saved statistic to
fill days that older versions skipped at request boundaries. It keeps the existing running total and checks
that Home Assistant saved the repaired rows. If a range or the import fails, it retries on a later day while
normal polling continues.

### Enable renewal on an existing installation

After updating the component to **0.5.0** or later and restarting Home Assistant, open the integration menu →
**Reconfigure**. Paste the `user.token` value from your logged-in browser into **Session token** and
submit. Home Assistant verifies meter access before storing it. An existing entry with only a JWT keeps
working until that JWT expires; reauthentication then asks for the session token.

## Using it in the Energy dashboard

Settings → Dashboards → Energy → Water consumption → **Add water source** → choose **MAST water meter** (the
`mast_water:meter_<card>` statistic).

## Limits

- MAST publishes **one cumulative reading per day**, so the dashboard has daily granularity and the newest
  reading is usually about a day old. Daily usage is each reading minus the previous one, as on the MAST site.
  Whether a reading is the meter value at the start or end of its date is not documented; compare against a
  bill if the exact day boundary matters to you.
- The statistic starts at zero at the first imported reading. If the meter is replaced and the reading drops,
  that day adds zero rather than a negative amount.
- Polls every 3 hours; new data only appears about once a day anyway.
- Automatic renewal depends on MAST continuing to accept the remembered-login credential. Its lifetime and
  behavior after an extended period of inactivity have not yet been measured.
- The MAST date picker goes back about two years, so that is how much history can be imported.

## Security

- The JWT contains a user identifier (probably your ID number) and your phone number in readable form (a JWT is
  only encoded, not encrypted). Redact it before sharing logs, screenshots or HAR files.
- The session token can obtain new JWTs. **Treat it as a password.** Home Assistant stores it in plain text
  in `.storage`; do not share backups or logs containing it. Its server lifetime is unknown.
- MAST may revoke the session renewal token after logout. It survived one observed logout and new OTP login,
  but its behavior across other sessions has not been established.
- Everything stays between your Home Assistant, MAST and your browser.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "MAST returned an error" when adding | Wrong card, a MAST backend problem, or unavailable meter data. HTTP 500 is generic and does not prove the token is invalid. |
| Re-authentication prompt every 2 days | Only a JWT is configured, or MAST rejected the session token. Add it through Reconfigure. |
| "MAST says the account assets are not valid" | MAST explicitly rejected the account's water assets during session restore; check the account and water access on the website. |
| Data stops, no prompt | MAST or session renewal may be temporarily unavailable. Network errors are retried without immediately requesting OTP. |
| A day is missing from older Energy history | Upgrade to the date-range fix and let the integration poll. It rereads and repairs the available history once. |
| Log: `MAST refused a token that has not expired (...); restored the session, trying again` | Normal. MAST had dropped its session state and the integration repaired it. |
| Log: `... retrying in 30 minutes (n/3)` | MAST still refused after the repair. After three tries in a row you are asked to log in again. |

For more detail, add this to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.mast_water: debug
```

## Reset history

Remove the integration, delete the `mast_water:meter_<card>` statistic (Developer tools → Statistics), delete
`.storage/mast_water.<card>`, and add the integration again.

## Development

`custom_components/mast_water/helpers.py` has no Home Assistant imports, so the parsing and statistics logic can
be unit tested on its own. `tests/` runs the config flow, setup, statistics import, session repair
and re-authentication inside a real Home Assistant with the MAST API mocked. Renewal tests cover expired JWTs,
opaque-token rotation, failure handling, and credential binding to a meter:

```bash
pip install pytest-homeassistant-custom-component
pytest
```

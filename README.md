# MAST Water for Home Assistant

Imports your daily water meter readings from the **MAST** resident portal (used by Mei Raanana and
other Israeli water corporations) into Home Assistant, including a backfill of your history, so the
data works in the **Energy dashboard** and in long-term statistics.

> **Unofficial.** It calls the same API the MAST web app uses, with your own session token, for your own
> data. MAST can change or block that at any time. Not affiliated with MAST or Mei Raanana.
> What was learned about the API, and why it works the way it does, is in
> [docs/FINDINGS.md](docs/FINDINGS.md).

## How it works, in one paragraph

MAST has no public API and logs you in with an SMS code plus a Google reCAPTCHA, so a script cannot
log in by itself (and this project deliberately does not try to). Instead **you log in yourself, in your
browser**, and the session token the website receives is handed to Home Assistant, either by pasting it or
automatically with the included userscript. The token lasts about **48 hours** and cannot be renewed
(see findings), so roughly every two days you log in again. Nothing is lost in between: the next time
Home Assistant has a valid token it fetches every day since the last import and fills the history in with
the correct dates.

## What you get

| Item | Meaning |
|---|---|
| Statistic `mast_water:meter_<card>` ("MAST water meter", m³) | The full history. **Use this in the Energy dashboard.** |
| Sensor *Meter reading* | Latest cumulative reading (m³) |
| Sensor *Latest daily usage* | Difference between the two most recent daily readings (m³) |
| Sensor *Token expires* (diagnostic) | When the current token stops working |

The sensors deliberately have no state class, so they do not create a second series that would double
count in the Energy dashboard.

## Install

1. Copy `custom_components/mast_water` into your Home Assistant `config/custom_components/`
   (or add this repository to HACS as a custom repository and install it there).
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → MAST Water.**

## Setup

### Step 1: first token (manual, one time)

1. Log in at <https://www.mast.co.il> and open your water consumption page.
2. Open browser DevTools → **Network**, click any request to `api.mast.co.il`, and copy the value of the
   `jwt` request header (with or without the `Bearer ` prefix).
3. Paste it into the integration. It then lists your water cards, so you pick one instead of typing a number.
4. The last screen shows the **token push URL** for the userscript (next section). You can show it again
   any time: integration menu → **Reconfigure**.

The first run imports up to two years of history in 90-day chunks. Chunks the portal rejects are skipped.

### Step 2 (optional): hands-free token refresh with the userscript

After this, logging in to mast.co.il in your normal browser is all you need to do every couple of days.

1. Install a userscript manager such as Tampermonkey in the browser you use for MAST.
2. Create a new script from [`userscript/mast-token-push.user.js`](userscript/mast-token-push.user.js).
3. Set `HA_WEBHOOK_URL` near the top to the URL from the integration, and replace `YOUR_HA_HOST` in the
   `@connect` line with its host name.
   - Use the **internal** URL if you usually log in from home, the **external** one if you want it to work anywhere.
4. Log in to MAST as usual. You will see a small "MAST token sent to Home Assistant" toast, and the
   *Token expires* sensor moves forward about 48 hours.

What the userscript does: it watches the requests the MAST site itself makes, notices the `jwt` header, and
posts the token to your webhook (once per new token). What it does **not** do: it does not log in, does not
fill or solve any captcha, and does not read SMS messages.

What Home Assistant does with a pushed token: it checks that it is a valid, unexpired JWT, that it is newer
than the stored one, and that it actually works for your card (a test request to MAST). Only then is it stored
and the integration reloaded, which also resolves any open "re-authenticate" prompt. A token from another
account, an expired token, or garbage is refused.

### If you skip the userscript

When the token expires Home Assistant shows a re-authentication prompt. Paste a fresh token and it carries on.

## Using it in the Energy dashboard

Settings → Dashboards → Energy → Water consumption → **Add water source** → choose
**MAST water meter** (the `mast_water:meter_<card>` statistic).

## Limits and good to know

- MAST publishes **one cumulative reading per day**, so the dashboard has daily granularity, and the newest
  reading is usually about a day old. Usage is attributed to the day of the reading's timestamp. I assumed
  the reading is the meter value at midnight on that date; compare against a bill or a day you know to confirm.
- The statistic starts at zero at the first imported reading. If the meter is replaced and the reading drops,
  that day adds zero rather than a negative amount.
- Polls every 6 hours; new data only appears about once a day anyway.
- Data older than what the portal keeps cannot be imported.

## Security notes

- The token is a login for your MAST account until it expires. **Logging out does not invalidate it**
  (the server only checks the signature and expiry), so treat it like a password for 48 hours.
- It also contains a user identifier (probably your ID number) and your phone number in readable form
  (a JWT is only encoded, not encrypted).
  Redact it before sharing logs, screenshots or HAR files.
- Home Assistant stores the token in `.storage` in plain text, like other integration credentials. Do not
  share backups.
- The webhook URL is a secret. Anyone with it can try to push a token, but only a token that works for your
  card is accepted. Regenerate it by removing and re-adding the integration.
- Everything is local to your Home Assistant, MAST and your browser. Nothing is sent anywhere else.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "MAST returned an error" when adding | Wrong card number. The API answers with a generic HTTP 500. |
| Re-authentication prompt every 2 days | Expected without the userscript. |
| Userscript toast never appears | `HA_WEBHOOK_URL` wrong or unreachable from the browser; open the browser console and look for `[mast-token-push]`. |
| Webhook answers 400 `token_not_valid_for_this_card` | The token belongs to a different MAST account than the configured card. |
| Data stops but no prompt | Token expired and a refresh is pending; check *Token expires*. |

## Reset history

Remove the integration, delete the `mast_water:meter_<card>` statistic (Developer tools → Statistics),
delete `.storage/mast_water.<card>`, and add the integration again.

## Development

`custom_components/mast_water/helpers.py` has no Home Assistant imports, so the parsing and statistics logic
can be unit tested on its own.

`tests/` runs the config flow, setup, statistics import, token webhook and re-authentication inside a real
Home Assistant with the MAST API mocked:

```bash
pip install pytest-homeassistant-custom-component
pytest
```

They were run against Home Assistant 2026.2.3 (17 tests passing). The userscript has no automated tests in this
repository; it was exercised with a fake browser environment in Node during development.

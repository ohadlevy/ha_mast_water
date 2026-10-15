"""Constants for the MAST Water integration."""

from datetime import timedelta

DOMAIN = "mast_water"

API_URL = "https://api.mast.co.il/mast/api/WaterMeter/GetWaterMeterCardReading"
CARDS_URL = "https://api.mast.co.il/mast/api/WaterMeter/GetWaterMeterCards"
ORIGIN = "https://www.mast.co.il"

CONF_CARD = "card_number"
CONF_TOKEN = "token"
CONF_COMPANY_ID = "company_id"
CONF_WEBHOOK_ID = "webhook_id"

# 110 = Mei Raanana (from the X-CompanyId header the MAST web app sends)
DEFAULT_COMPANY_ID = "110"

UPDATE_INTERVAL = timedelta(hours=6)

# How far back the very first run asks for data. The web app's date picker allows
# about two years. Empty or failing old chunks are skipped.
BACKFILL_DAYS = 730
# The web app limits one request to 30 days (its default window is 30 days
# inclusive), or only 7 days for water corporations whose meters come from the
# supplier "Arad" or "AradHarmony". Start with 30 and fall back to 7 on HTTP 400.
CHUNK_DAYS = 30
SMALL_CHUNK_DAYS = 7

STORE_VERSION = 1

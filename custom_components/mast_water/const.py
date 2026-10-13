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

# How far back the very first run asks for data. Empty or failing chunks are
# skipped, so asking for more than the portal holds is harmless.
BACKFILL_DAYS = 730
CHUNK_DAYS = 90

STORE_VERSION = 1

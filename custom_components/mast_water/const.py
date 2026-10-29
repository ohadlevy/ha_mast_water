"""Constants for the MAST Water integration."""

from datetime import timedelta

DOMAIN = "mast_water"

API_BASE = "https://api.mast.co.il/mast/api/"
API_URL = API_BASE + "WaterMeter/GetWaterMeterCardReading"
CARDS_URL = API_BASE + "WaterMeter/GetWaterMeterCards"
VALIDATE_URL = API_BASE + "PersonalAreaAsset/ValidateUserAssets"
RESUME_URL = API_BASE + "Users/GetUserByToken"
ORIGIN = "https://www.mast.co.il"

CONF_CARD = "card_number"
CONF_TOKEN = "token"
CONF_USER_TOKEN = "user_token"
CONF_COMPANY_ID = "company_id"

# 110 = Mei Raanana (from the X-CompanyId header the MAST web app sends)
DEFAULT_COMPANY_ID = "110"

# New data appears about once a day.
UPDATE_INTERVAL = timedelta(hours=3)
# Renew while the JWT is still usable, with several polls available for retries.
TOKEN_RENEWAL_WINDOW = timedelta(hours=12)

# How far back the very first run asks for data. The web app's date picker allows
# about two years. Empty or failing old chunks are skipped.
BACKFILL_DAYS = 730
# MAST's dateTo is exclusive. The web app limits one request to a 30-day span,
# or a 7-day span for water corporations whose meters come from the supplier
# "Arad" or "AradHarmony". Start with 30 and fall back to 7 on HTTP 400.
CHUNK_DAYS = 30
SMALL_CHUNK_DAYS = 7

# If MAST still refuses a token that has not expired, even after the session was restored,
# retry later this many times before asking the user to log in again.
AUTH_STRIKES = 3
AUTH_RETRY_SECONDS = 1800

STORE_VERSION = 1
STATISTICS_REPAIR_VERSION = 3

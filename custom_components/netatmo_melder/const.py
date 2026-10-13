"""Constants for the netatmo_melder integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "netatmo_melder"
MANUFACTURER: Final = "Netatmo"

OAUTH2_AUTHORIZE: Final = "https://api.netatmo.com/oauth2/authorize"
OAUTH2_TOKEN: Final = "https://api.netatmo.com/oauth2/token"
API_BASE: Final = "https://api.netatmo.com/api/"
SCOPES: Final = ("read_smokedetector", "read_carbonmonoxidedetector")
REQUEST_TIMEOUT: Final = 15

MODEL_SMOKE: Final = "NSD"
MODEL_CO: Final = "NCO"
SUPPORTED_MODELS: Final = frozenset({MODEL_SMOKE, MODEL_CO})

# Netatmo allows 50 requests / 10 s and 500 / h; one poll = homestatus + getevents.
SCAN_INTERVAL: Final = timedelta(minutes=5)
# Backfill (first refresh after setup/restart) must not push alarms older than this (K5b).
# In live operation every newly seen alarm is pushed, however late.
ALARM_MAX_AGE: Final = timedelta(minutes=10)
# last_seen only changes on events (days to weeks, Phase 0), so "offline" needs a long threshold.
OFFLINE_AFTER: Final = timedelta(days=45)

CONF_CLOUDHOOK_URL: Final = "cloudhook_url"
WEBHOOK_SIGNATURE_HEADER: Final = "X-Netatmo-secret"
WEBHOOK_ACTIVATION: Final = "webhook_activation"
# At most one webhook-triggered refresh per interval (polling is not affected).
WEBHOOK_REFRESH_MIN_INTERVAL: Final = timedelta(seconds=30)
# getevents can lag behind the webhook: re-check for the webhook's event_id this often.
WEBHOOK_EVENT_RETRIES: Final = 2
WEBHOOK_EVENT_RETRY_DELAY: Final = timedelta(seconds=20)
WEBHOOK_SAVE_DELAY: Final = 30  # seconds; Store write of the last webhook time
WEBHOOK_REPLAY_CACHE_SIZE: Final = 256
ADDWEBHOOK_RETRY_DELAY: Final = timedelta(minutes=5)

EVENT_ALARM: Final = f"{DOMAIN}_alarm"

STORAGE_VERSION: Final = 1

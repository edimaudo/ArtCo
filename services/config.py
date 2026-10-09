from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

QLOO_BASE_URL = "https://hackathon.api.qloo.com"
QLOO_API_KEY = os.getenv("QLOO_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
ARTSY_XAPP_TOKEN = os.getenv("ARTSY_XAPP_TOKEN", "")
ARTSY_PARTNER_ID = os.getenv("ARTSY_PARTNER_ID", "")
ARTLOGIC_FEED_URL = os.getenv("ARTLOGIC_FEED_URL", "")
COLLECT24_API_KEY = os.getenv("COLLECT24_API_KEY", "")
COLLECT24_BASE_URL = os.getenv("COLLECT24_BASE_URL", "https://collect24.art/api/v1")
ARTCO_BASE_URL = os.getenv("ARTCO_BASE_URL", "http://localhost:8000")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_APPLICATION_FEE_BPS = int(os.getenv("STRIPE_APPLICATION_FEE_BPS", "0"))
try:
    STRIPE_CHECKOUT_CATALOG = __import__("json").loads(os.getenv("ARTCO_STRIPE_CHECKOUT_CATALOG", "{}"))
except Exception:
    STRIPE_CHECKOUT_CATALOG = {}
ARTWORK_RESULT_LIMIT = int(os.getenv("ARTWORK_RESULT_LIMIT", "18"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "6"))
QLOO_SEARCH_LIMIT = int(os.getenv("QLOO_SEARCH_LIMIT", "5"))
QLOO_ARTIST_TAKE = int(os.getenv("QLOO_ARTIST_TAKE", "12"))

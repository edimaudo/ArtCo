from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

QLOO_BASE_URL = os.getenv("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
QLOO_API_KEY = os.getenv("QLOO_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ARTSY_XAPP_TOKEN = os.getenv("ARTSY_XAPP_TOKEN", "")
ARTSY_PARTNER_ID = os.getenv("ARTSY_PARTNER_ID", "")
ARTLOGIC_FEED_URL = os.getenv("ARTLOGIC_FEED_URL", "")
ARTWORK_RESULT_LIMIT = int(os.getenv("ARTWORK_RESULT_LIMIT", "18"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "6"))
QLOO_SEARCH_LIMIT = int(os.getenv("QLOO_SEARCH_LIMIT", "5"))
QLOO_ARTIST_TAKE = int(os.getenv("QLOO_ARTIST_TAKE", "12"))

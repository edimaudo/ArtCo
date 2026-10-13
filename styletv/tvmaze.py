"""TVmaze catalogue adapter.

TVmaze's public API data is licensed under CC BY-SA and requires attribution.
The UI links to TVmaze as the data source and uses only poster URLs returned by
its API. This adapter caps the recommendation catalogue at 450 shows.
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from urllib.parse import quote
from copy import deepcopy
from typing import Any

import httpx

TVMAZE_BASE = "https://api.tvmaze.com"
TVMAZE_API_URL = "https://www.tvmaze.com/api"
MAX_CATALOG_SHOWS = 450
# Spread the public index requests across its ID-paginated catalogue so selection
# is not limited to the oldest TVmaze IDs. Eight index pages + six exact title
# lookups = 14 parallel requests, below TVmaze's documented baseline rate limit.
TVMAZE_INDEX_PAGES = (0, 25, 50, 75, 100, 125, 150, 175)
CACHE_TTL_SECONDS = 60 * 60
FALLBACK_TTL_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 2.5

_cache: dict[str, Any] = {"expires": 0.0, "payload": None}


def _normalise_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<\s*br\s*/?\s*>", " ", value, flags=re.I)
    text = re.sub(r"</p\s*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _style_tags(name: str, genres: list[str], summary: str, seed: dict[str, Any] | None = None) -> list[str]:
    if seed:
        return list(seed.get("tags") or [])

    text = f"{name} {' '.join(genres)} {summary}".casefold()
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("alternative", ("alternative", "punk", "goth", "grunge", "rebellion", "misfit", "outcast", "underground")),
        ("punk", ("punk", "rock band", "punk rock", "rebellion", "subculture")),
        ("retro", ("retro", "vintage", "nostalgia", "1980", "80s", "1970", "70s", "1990", "90s", "period piece", "historical")),
        ("streetwear", ("streetwear", "skate", "teenage", "high school", "youth culture", "urban fashion")),
        ("tailoring", ("tailoring", "suit", "business", "corporate", "lawyer", "politician", "high society", "elite")),
        ("utility", ("survival", "post-apocalyptic", "military", "police", "detective", "war", "outbreak", "investigation")),
        ("layering", ("layered", "layers", "workplace", "family ensemble", "adventure")),
        ("experimental", ("experimental", "avant-garde", "artist", "fashion", "drag", "performer", "music scene", "eccentric")),
        ("minimal", ("minimal", "minimalist", "office", "chef", "restaurant", "understated")),
        ("preppy", ("preppy", "boarding school", "prep school", "college", "university")),
        ("bold", ("glamour", "extravagant", "colorful", "colourful", "bold fashion", "couture", "statement looks")),
        ("workwear", ("workwear", "kitchen", "chef", "uniform", "workplace", "restaurant")),
    ]
    matches = [tag for tag, words in rules if any(word in text for word in words)]
    # Genre terms remain searchable, but aren't silently treated as style advice.
    return matches[:6]


def _score(record: dict[str, Any]) -> float:
    rating = ((record.get("rating") or {}).get("average") or 0) if isinstance(record.get("rating"), dict) else 0
    weight = record.get("weight") or 0
    summary = record.get("summary") or ""
    score = float(weight) * 0.7 + float(rating) * 3
    if record.get("image"):
        score += 12
    if summary:
        score += 5
    if record.get("status") == "Running":
        score += 2
    if record.get("language") == "English":
        score += 2
    return score


def _normalise_record(record: dict[str, Any], seed: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not isinstance(record, dict) or not record.get("name"):
        return None
    tvmaze_id = record.get("id")
    if not tvmaze_id:
        return None
    image = record.get("image") if isinstance(record.get("image"), dict) else {}
    genres = record.get("genres") if isinstance(record.get("genres"), list) else []
    genres = [str(genre) for genre in genres if genre]
    summary = _strip_html(record.get("summary"))
    name = str(record["name"]).strip()
    try:
        numeric_rating = (record.get("rating") or {}).get("average")
    except AttributeError:
        numeric_rating = None

    if seed:
        show_id = seed["id"]
        wornontv_url = seed.get("wornontv_url") or "https://wornontv.net/shows/"
        wornontv_direct = bool(seed.get("wornontv_direct"))
        style_note = seed.get("description", "")
    else:
        show_id = f"tvmaze-{tvmaze_id}"
        wornontv_url = "https://wornontv.net/shows/"
        wornontv_direct = False
        style_note = ""

    description = summary[:340].rstrip()
    if len(summary) > 340:
        description = description.rsplit(" ", 1)[0] + "…"
    if not description:
        description = ", ".join(genres) if genres else "TV series"

    return {
        "id": show_id,
        "tvmaze_id": int(tvmaze_id),
        "name": name,
        "description": description,
        "style_note": style_note,
        "tags": _style_tags(name, genres, summary, seed),
        "tags_source": "curated" if seed else "inferred",
        "genres": genres,
        "language": record.get("language"),
        "status": record.get("status"),
        "premiered": record.get("premiered"),
        "rating": numeric_rating,
        "network": ((record.get("network") or {}).get("name") if isinstance(record.get("network"), dict) else None)
            or ((record.get("webChannel") or {}).get("name") if isinstance(record.get("webChannel"), dict) else None),
        "image": image.get("medium") or image.get("original"),
        "tvmaze_url": record.get("url") or f"https://www.tvmaze.com/shows/{tvmaze_id}",
        "data_source": "TVmaze",
        "wornontv_url": wornontv_url,
        "wornontv_direct": wornontv_direct,
    }


async def _fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


async def get_show_catalog(seed_shows: list[dict[str, Any]]) -> dict[str, Any]:
    """Load at most 450 show records, cached in a warm serverless instance.

    The TVmaze index is paginated at 250 shows per page by show ID. Several
    index pages sampled across the ID range provide a broader pool than the first
    500 IDs alone; six known starter titles are looked up separately and retained.
    The final catalogue is ranked and hard-capped at 450 records. When TVmaze is
    unavailable, return the six starter records and retry shortly rather than
    caching failure for an hour.
    """
    now = time.monotonic()
    cached = _cache.get("payload")
    if cached and now < float(_cache.get("expires", 0)):
        return deepcopy(cached)

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=REQUEST_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "styleTV/0.2 (TVmaze open data consumer)", "Accept": "application/json"},
        ) as client:
            urls = [f"{TVMAZE_BASE}/shows?page={page}" for page in TVMAZE_INDEX_PAGES]
            urls.extend(f"{TVMAZE_BASE}/singlesearch/shows?q={quote(show['name'])}" for show in seed_shows)
            results = await asyncio.gather(*[_fetch_json(client, url) for url in urls], return_exceptions=True)
    except Exception:
        results = []

    records: dict[int, dict[str, Any]] = {}
    successful_index_pages = 0
    index_page_count = len(TVMAZE_INDEX_PAGES)
    for result in (results[:index_page_count] if results else []):
        if isinstance(result, list):
            successful_index_pages += 1
            for record in result:
                if isinstance(record, dict) and record.get("id"):
                    records[int(record["id"])] = record

    lookup_records: dict[str, dict[str, Any]] = {}
    for seed, result in zip(seed_shows, results[index_page_count:] if results else []):
        if not isinstance(result, dict):
            continue
        if _normalise_title(str(result.get("name", ""))) == _normalise_title(seed["name"]):
            lookup_records[_normalise_title(seed["name"])] = result
            if result.get("id"):
                records[int(result["id"])] = result

    # Retain each starter show under its stable ID so IndexedDB favourites stay valid.
    output: list[dict[str, Any]] = []
    starter_titles: set[str] = set()
    for seed in seed_shows:
        key = _normalise_title(seed["name"])
        record = lookup_records.get(key)
        if record is None:
            record = next((r for r in records.values() if _normalise_title(str(r.get("name", ""))) == key), None)
        show = _normalise_record(record, seed) if record else None
        if not show:
            show = {
                **seed,
                "tvmaze_id": None,
                "genres": [],
                "tags_source": "curated",
                "language": "English",
                "status": None,
                "premiered": None,
                "rating": None,
                "network": None,
                "tvmaze_url": "https://www.tvmaze.com/",
                "data_source": "starter-fallback",
            }
        output.append(show)
        starter_titles.add(key)

    remaining = [
        record for record in records.values()
        if _normalise_title(str(record.get("name", ""))) not in starter_titles
        and record.get("image")
        and record.get("name")
    ]
    remaining.sort(key=_score, reverse=True)
    for record in remaining:
        if len(output) >= MAX_CATALOG_SHOWS:
            break
        normalized = _normalise_record(record)
        if normalized:
            output.append(normalized)

    source_status = "live" if successful_index_pages == index_page_count else ("partial" if successful_index_pages or lookup_records else "fallback")
    payload = {
        "shows": output[:MAX_CATALOG_SHOWS],
        "catalogue": {
            "count": min(len(output), MAX_CATALOG_SHOWS),
            "limit": MAX_CATALOG_SHOWS,
            "source": "TVmaze",
            "source_url": TVMAZE_API_URL,
            "license": "CC BY-SA",
            "source_status": source_status,
        },
    }
    cache_seconds = CACHE_TTL_SECONDS if source_status == "live" else FALLBACK_TTL_SECONDS
    _cache["payload"] = deepcopy(payload)
    _cache["expires"] = time.monotonic() + cache_seconds
    return payload


def clear_show_catalog_cache() -> None:
    """Reset the process-local cache, intended for deterministic tests."""
    _cache.update({"expires": 0.0, "payload": None})

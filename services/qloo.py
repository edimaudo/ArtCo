from __future__ import annotations

from typing import Any

import httpx

from .config import QLOO_API_KEY, QLOO_ARTIST_TAKE, QLOO_BASE_URL, QLOO_SEARCH_LIMIT, REQUEST_TIMEOUT

SEARCH_TYPES = [
    "urn:entity:artist",
    "urn:entity:movie",
    "urn:entity:tv_show",
    "urn:entity:brand",
    "urn:entity:destination",
    "urn:entity:place",
    "urn:entity:book",
]


def _headers() -> dict[str, str]:
    return {"X-Api-Key": QLOO_API_KEY, "Accept": "application/json"}


async def search_entities(query: str) -> list[dict[str, Any]]:
    if not QLOO_API_KEY or not query.strip():
        return []
    params: list[tuple[str, str]] = [("query", query.strip())]
    for entity_type in SEARCH_TYPES:
        params.append(("types", entity_type))
    params.extend([("take", str(QLOO_SEARCH_LIMIT)), ("sort_by", "match")])

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(f"{QLOO_BASE_URL}/search", headers=_headers(), params=params)
        response.raise_for_status()
        return _extract_entities(response.json())


async def get_artist_insights(entity_ids: list[str], discovery_level: int) -> list[dict[str, Any]]:
    if not QLOO_API_KEY or not entity_ids:
        return []

    trend_bias = "low" if discovery_level < 35 else "medium" if discovery_level < 70 else "high"
    cross_domain = "6" if discovery_level < 35 else "12" if discovery_level < 70 else "20"
    params: list[tuple[str, str]] = [
        ("filter.type", "urn:entity:artist"),
        ("signal.interests.entities", ",".join(entity_ids)),
        ("take", str(QLOO_ARTIST_TAKE)),
        ("bias.trends", trend_bias),
        ("backfill.cross_domain.take", cross_domain),
        ("feature.explainability", "true"),
    ]

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(f"{QLOO_BASE_URL}/v2/insights", headers=_headers(), params=params)
        response.raise_for_status()
        return _extract_entities(response.json())


def _extract_entities(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = payload.get("entities") or payload.get("results") or payload.get("data") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("entities") or candidates.get("results") or []
    output: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties") or {}
        query_info = item.get("query") or {}
        output.append(
            {
                "id": item.get("entity_id") or item.get("id"),
                "name": item.get("name") or properties.get("name") or "Unknown",
                "type": item.get("type") or item.get("subtype"),
                "affinity": query_info.get("affinity") or item.get("affinity"),
                "image": properties.get("image") or item.get("image"),
            }
        )
    return output

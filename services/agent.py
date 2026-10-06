from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from models.schemas import ConciergeRequest
from .art_sources import Artwork, commercial_providers, institutional_providers
from .llm import plan_art_searches
from .qloo import get_artist_insights, search_entities


async def _resolve_entities(request: ConciergeRequest, status: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Resolve user-entered cultural references through Qloo's search endpoint."""
    status.append({"label": "Understanding your cultural references", "state": "active"})
    queries = list(dict.fromkeys(request.loves + request.art_interests + request.additional_interests))[:12]
    resolved: list[dict[str, Any]] = []

    async def resolve(query: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return query, await search_entities(query)
        except Exception:
            return query, []

    results = await asyncio.gather(*(resolve(query) for query in queries))
    for query, matches in results:
        if matches:
            resolved.append({"query": query, "match": matches[0]})

    status[-1]["state"] = "done"
    return resolved


async def _qloo_taste(resolved: list[dict[str, Any]], request: ConciergeRequest, status: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Ask Qloo for related artists using the resolved cultural entities as interests."""
    status.append({"label": "Exploring your taste with Qloo", "state": "active"})
    entity_ids = [
        item["match"].get("id")
        for item in resolved
        if item.get("match", {}).get("id")
    ]

    try:
        artists = await get_artist_insights(entity_ids[:8], request.discovery_level)
    except Exception:
        artists = []

    status[-1]["state"] = "done"
    return artists


def _deterministic_directions(request: ConciergeRequest, qloo_artists: list[dict[str, Any]]) -> list[str]:
    """Create a safe fallback search plan when an LLM is unavailable."""
    directions: list[str] = []
    artist_names = [str(item.get("name")) for item in qloo_artists if item.get("name")]

    directions.extend(request.art_interests[:4])
    directions.extend(request.mediums[:3])
    directions.extend(artist_names[:5])

    if request.discovery_level >= 70:
        directions.extend(["experimental contemporary art", "emerging artists", "cross-disciplinary art"])
    elif request.discovery_level <= 30:
        directions.extend(["modern art", "minimalist art", "figurative contemporary art"])
    else:
        directions.extend(["contemporary art", "modern painting", "contemporary photography"])

    if request.preferred_market:
        directions.append(f"art from {request.preferred_market}")

    if request.feedback:
        directions.append(request.feedback)

    if not directions:
        directions = ["contemporary art", "modern art", "painting", "photography"]

    return list(dict.fromkeys(direction.strip() for direction in directions if direction.strip()))[:10]


async def _build_search_plan(
    request: ConciergeRequest,
    qloo_artists: list[dict[str, Any]],
    status: list[dict[str, str]],
) -> list[str]:
    """Plan artwork searches using Gemini when configured, otherwise deterministic logic."""
    status.append({"label": "Planning the artwork search", "state": "active"})
    try:
        llm_directions = await plan_art_searches(
            request.intent.value,
            request.loves + request.additional_interests,
            request.art_interests,
            qloo_artists,
            request.discovery_level,
            feedback=request.feedback,
            room=request.room,
            budget_max=request.budget_max,
        )
    except Exception:
        llm_directions = []

    directions = list(dict.fromkeys(llm_directions + _deterministic_directions(request, qloo_artists)))[:6]
    status[-1]["state"] = "done"
    return directions


def _budget_ok(work: Artwork, request: ConciergeRequest) -> bool:
    if work.price is None:
        return True
    if request.budget_min is not None and work.price < request.budget_min:
        return False
    if request.budget_max is not None and work.price > request.budget_max:
        return False
    return True


def _relevance_score(work: Artwork, request: ConciergeRequest, qloo_artists: list[dict[str, Any]], direction: str) -> float:
    score = 0.0
    haystack = " ".join(
        part for part in [work.title, work.artist, work.medium, work.description, direction] if part
    ).lower()

    for artist in qloo_artists:
        name = str(artist.get("name") or "").lower()
        if name and name in work.artist.lower():
            score += 8.0

    for interest in request.art_interests + request.mediums:
        if interest.lower() in haystack:
            score += 2.5

    if direction and any(token.lower() in haystack for token in direction.split() if len(token) > 3):
        score += 1.5

    if work.image_url:
        score += 1.0
    if request.purchase_required:
        score += 8.0 if work.availability == "available" else -6.0
    if request.discovery_level >= 70:
        score += 1.5 if direction not in request.art_interests else 0.0
    elif request.discovery_level <= 30:
        score += 1.0 if any(interest.lower() in haystack for interest in request.art_interests) else 0.0

    if request.room and work.dimensions:
        score += 0.5

    return score


async def _search_provider(provider: Any, direction: str, limit: int) -> list[Artwork]:
    try:
        works = await provider.search(direction, limit=limit)
        return [replace(work, matched_direction=direction) for work in works]
    except Exception:
        return []


async def _research(
    directions: list[str],
    request: ConciergeRequest,
    status: list[dict[str, str]],
) -> list[Artwork]:
    """Research an initial candidate set. Purchase requests prioritize commercial inventory."""
    status.append({"label": "Researching artwork sources", "state": "active"})
    commercial = commercial_providers()
    institutional = institutional_providers()

    provider_groups = [commercial, institutional] if request.purchase_required else [institutional, commercial]
    works: list[Artwork] = []

    # Bound the first pass so a slow/unavailable provider cannot dominate the request.
    search_directions = directions[:4]
    for providers in provider_groups:
        tasks = [
            _search_provider(provider, direction, limit=5)
            for provider in providers
            for direction in search_directions
        ]
        if tasks:
            batches = await asyncio.gather(*tasks)
            works.extend(item for batch in batches for item in batch)

    deduped: dict[str, Artwork] = {}
    for work in works:
        if _budget_ok(work, request):
            deduped.setdefault(work.id, work)

    status[-1]["state"] = "done"
    return list(deduped.values())


def _select(works: list[Artwork], request: ConciergeRequest, qloo_artists: list[dict[str, Any]]) -> list[Artwork]:
    ranked = sorted(
        works,
        key=lambda work: _relevance_score(work, request, qloo_artists, work.matched_direction or ""),
        reverse=True,
    )

    seen_artists: set[str] = set()
    selected: list[Artwork] = []
    for work in ranked:
        artist_key = work.artist.strip().lower()
        # Prefer variety rather than filling the shortlist with one artist.
        if artist_key and artist_key in seen_artists and len(selected) < 4:
            continue
        selected.append(work)
        if artist_key:
            seen_artists.add(artist_key)
        if len(selected) >= max(1, request.number_of_works + 4):
            break
    return selected


def _should_broaden(works: list[Artwork], request: ConciergeRequest) -> bool:
    target = 6
    if request.intent.value == "taste":
        target = 4
    if request.purchase_required:
        available = sum(work.availability == "available" for work in works)
        return available < 3
    return len(works) < target


async def run_concierge(request: ConciergeRequest) -> dict[str, Any]:
    """Run the native Python concierge loop without a graph framework."""
    status: list[dict[str, str]] = [
        {"label": "Brief received", "state": "done"},
    ]

    resolved = await _resolve_entities(request, status)
    qloo_artists = await _qloo_taste(resolved, request, status)
    directions = await _build_search_plan(request, qloo_artists, status)

    works = await _research(directions, request, status)

    # One adaptive second pass is deliberate: the concierge can change its search
    # strategy when the first pass does not satisfy the user's objective.
    if _should_broaden(works, request):
        status.append({"label": "Expanding the search", "state": "active"})
        broader = list(dict.fromkeys(directions + ["emerging contemporary artists", "adjacent contemporary art"]))
        second_pass = await _research(broader[-4:], request, status)
        works.extend(second_pass)
        status[-1]["state"] = "done"

    deduped: dict[str, Artwork] = {work.id: work for work in works}
    works = list(deduped.values())

    status.append({"label": "Evaluating candidates against your brief", "state": "active"})
    selected = _select(works, request, qloo_artists)
    status[-1]["state"] = "done"

    purchasable = [work for work in selected if work.availability == "available" and work.source_kind == "commercial"]
    not_for_sale = [work for work in selected if work.availability != "available" or work.source_kind != "commercial"]

    # Buy means purchase inventory first. Never relabel an institutional work as purchasable.
    if request.purchase_required:
        primary = purchasable[:request.number_of_works]
        secondary = not_for_sale[:4]
    else:
        primary = selected[:request.number_of_works] if request.intent.value == "curate" else selected[:8]
        secondary = not_for_sale[:4]

    status.append({"label": "Preparing your shortlist", "state": "done"})

    intent_summary = {
        "discover": "I used your broader cultural references to explore artistic territory beyond the obvious.",
        "find": "I translated your brief into search directions and narrowed the results to works that fit the criteria you gave me.",
        "taste": "I started close to the preferences you gave me, then explored related artists and directions that can help you discover what you respond to.",
        "curate": "I looked for works that fit your taste and can make sense together in the context you described.",
        "buy": "I prioritised artwork marked as available from commercial sources. Institutional works are kept separate as cultural references.",
        "learn": "I researched the artistic direction you asked about and selected works that help you understand it in context.",
    }[request.intent.value]

    commercial_found = sum(work.source_kind == "commercial" for work in works)
    institutional_found = sum(work.source_kind == "institution" for work in works)

    return {
        "success": True,
        "intent": request.intent.value,
        "summary": intent_summary,
        "taste": {
            "inputs": request.loves + request.additional_interests,
            "resolved": resolved[:10],
            "related_artists": qloo_artists[:8],
        },
        "search_directions": directions,
        "results": [_serialize(work, request, qloo_artists) for work in primary],
        "not_for_sale": [_serialize(work, request, qloo_artists) for work in secondary],
        "purchase_available": len(purchasable),
        "commercial_found": commercial_found,
        "institutional_found": institutional_found,
        "total_found": len(works),
        "status": status,
    }


def _serialize(work: Artwork, request: ConciergeRequest, qloo_artists: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    matching_artist = next(
        (item for item in qloo_artists if item.get("name") and str(item.get("name")).lower() in work.artist.lower()),
        None,
    )
    if matching_artist:
        affinity = matching_artist.get("affinity")
        if isinstance(affinity, (int, float)):
            reasons.append("Connected to your wider cultural taste")
        else:
            reasons.append("Connected to your wider cultural taste")
    if request.art_interests and work.medium and any(item.lower() in work.medium.lower() for item in request.art_interests):
        reasons.append("matches your stated art interests")
    if request.mediums and work.medium and any(item.lower() in work.medium.lower() for item in request.mediums):
        reasons.append("fits your preferred medium")
    if request.discovery_level >= 70:
        reasons.append("chosen with room for discovery")
    elif request.discovery_level <= 30:
        reasons.append("kept close to your stated preferences")
    if not reasons:
        reasons.append("fits the direction the concierge researched for you")

    return {
        "id": work.id,
        "title": work.title,
        "artist": work.artist,
        "image_url": work.image_url,
        "detail_url": work.detail_url,
        "source": work.source,
        "source_kind": work.source_kind,
        "availability": work.availability,
        "price": work.price,
        "currency": work.currency,
        "medium": work.medium,
        "dimensions": work.dimensions,
        "year": work.year,
        "description": work.description,
        "matched_direction": work.matched_direction,
        "why": reasons[:2],
        "price_label": (
            f"{work.currency} {work.price:,.0f}" if work.price is not None and work.currency else
            f"{work.price:,.0f}" if work.price is not None else
            "Price on request" if work.source_kind == "commercial" and work.availability == "available" else
            None
        ),
    }

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any

from models.schemas import ConciergeRequest
from .art_sources import Artwork, commercial_providers, institutional_providers, _normalise_image_url, _usable_image_url
from .image_proxy import display_image_url, is_allowed_detail_url
from .llm import CandidateReview, RequestSignals, analyze_inspiration_images, extract_request_signals, plan_art_searches
from .qloo import get_artist_insights, search_entities
from .payments import checkout_catalog_entry
from . import config

logger = logging.getLogger(__name__)


async def _resolve_entities(
    request: ConciergeRequest,
    status: list[dict[str, str]],
    signals: Any,
) -> list[dict[str, Any]]:
    """Resolve both saved taste references and important references in the current request."""
    progress = {"label": "Understanding what matters to you", "state": "active"}
    status.append(progress)
    # Only positive cultural references belong in the taste graph. Art subjects
    # (for example, "birds") are sent to artwork catalogues, while "avoid" values
    # must never become positive Qloo signals. Resolving the entire natural-language
    # request also creates weak cross-domain matches, so generic prompts are skipped.
    avoid_lookup = {str(value).strip().casefold() for value in request.avoid_references if str(value).strip()}
    positive_references = [
        *request.loves,
        *signals.cultural_references,
        *request.additional_interests,
    ]
    queries = [
        value for value in dict.fromkeys(str(value).strip() for value in positive_references)
        if value and value.casefold() not in avoid_lookup
    ][:6]

    async def resolve(query: str) -> tuple[str, list[dict[str, Any]], str | None]:
        try:
            return query, await search_entities(query), None
        except Exception as exc:
            return query, [], str(exc)


    results = await asyncio.gather(*(resolve(query) for query in queries)) if queries else []
    resolved: list[dict[str, Any]] = []
    errors: list[str] = []
    for query, matches, error in results:
        if error:
            errors.append(error)
        if matches:
            resolved.append({"query": query, "match": matches[0]})
    progress["state"] = "done"
    if errors:
        logger.warning("Qloo entity resolution failed for %d queries; first error: %s", len(errors), errors[0])
        status.append({"label": "Cultural discovery service unavailable", "state": "warning", "detail": errors[0][:160]})
    elif not config.QLOO_API_KEY:
        status.append({"label": "Cultural discovery service not configured", "state": "warning"})
    return resolved


async def _qloo_taste(
    resolved: list[dict[str, Any]],
    request: ConciergeRequest,
    status: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Use Qloo to expand cultural references into related artists."""
    progress = {"label": "Connecting your tastes to art", "state": "active"}
    status.append(progress)
    entity_ids = [
        item["match"].get("id")
        for item in resolved
        if item.get("match", {}).get("id")
    ]
    try:
        artists = await get_artist_insights(entity_ids[:8], request.discovery_level)
    except Exception as exc:
        artists = []
        logger.warning("Qloo Insights failed (%s)", type(exc).__name__)
        progress["state"] = "warning"
        progress["detail"] = str(exc)[:160]
        return artists
    progress["state"] = "done"
    return artists


async def _build_search_plan(
    request: ConciergeRequest,
    signals: Any,
    qloo_artists: list[dict[str, Any]],
    status: list[dict[str, str]],
) -> list[str]:
    progress = {"label": "Working out where to search", "state": "active"}
    status.append(progress)
    try:
        plan = await plan_art_searches(
            ("taste" if _uses_saved_taste(request) else request.intent.value),
            request.goal or "",
            signals.cultural_references,
            list(dict.fromkeys([*request.art_interests, *signals.art_interests, *_taste_reference_directions(request)])),
            list(dict.fromkeys(request.mediums + signals.mediums)),
            qloo_artists,
            request.discovery_level,
            feedback=request.feedback or "",
            room=request.room or signals.room,
            budget_max=request.budget_max if request.budget_max is not None else signals.budget_max,
            preferred_market=request.preferred_market or signals.market,
        )
        directions = plan.queries
    except Exception as exc:
        logger.warning("Search planning failed (%s); deterministic planning will be used", type(exc).__name__)
        directions = []
        progress["state"] = "warning"
        progress["detail"] = "Using a general art-search plan."
    else:
        progress["state"] = "done"

    # Never let a missing/invalid model response turn a valid concierge request
    # into zero artwork-provider calls. Add broad catalogue-friendly directions
    # even when the cultural profile is sparse or Qloo/Gemini returns no data.
    fallback_by_intent = {
        "buy": ["contemporary art for sale", "original contemporary painting", "limited edition art print"],
        "curate": ["contemporary paintings available to buy", "art photography for sale", "modern sculpture for sale"],
        "taste": ["artworks for sale related to your saved taste", "contemporary art available to buy", "abstract art for sale"],
        "learn": ["art history", "modern art", "contemporary art"],
    }
    fallbacks = fallback_by_intent["taste"] if _uses_saved_taste(request) else fallback_by_intent.get(
        request.intent.value,
        ["contemporary art", "modern painting", "contemporary photography"],
    )
    combined = list(dict.fromkeys([*(directions or []), *fallbacks]))
    return [item.strip() for item in combined if isinstance(item, str) and item.strip()][:8]


def _apply_signal_overrides(request: ConciergeRequest, signals: Any) -> ConciergeRequest:
    """Merge natural-language brief signals without overwriting explicit structured fields."""
    values = {}
    if not request.art_interests and signals.art_interests:
        values["art_interests"] = signals.art_interests
    if not request.mediums and signals.mediums:
        values["mediums"] = signals.mediums
    if request.room is None and signals.room:
        values["room"] = signals.room
    if request.size_preference is None and signals.size_preference:
        values["size_preference"] = signals.size_preference
    if request.preferred_market is None and signals.market:
        values["preferred_market"] = signals.market
    if request.budget_min is None and signals.budget_min is not None:
        values["budget_min"] = signals.budget_min
    if request.budget_max is None and signals.budget_max is not None:
        values["budget_max"] = signals.budget_max
    if request.intent.value == "curate" and request.number_of_works == 1 and signals.number_of_works:
        values["number_of_works"] = signals.number_of_works
    return request.model_copy(update=values) if values else request


def _budget_ok(work: Artwork, request: ConciergeRequest) -> bool:
    if work.price is None:
        return True
    if request.budget_min is not None and work.price < request.budget_min:
        return False
    if request.budget_max is not None and work.price > request.budget_max:
        return False
    return True


_TOKEN_STOP_WORDS = {
    "about", "after", "along", "also", "and", "anything", "around", "because",
    "before", "been", "being", "between", "could", "from", "have", "into",
    "just", "like", "looking", "make", "more", "most", "much", "need",
    "over", "please", "show", "some", "that", "their", "them", "there",
    "these", "they", "this", "those", "through", "want", "what", "when",
    "where", "which", "with", "would", "your", "find", "give", "help",
    "work", "works", "art", "piece", "pieces",
}

def _tokenize(text: str | None) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", (text or "").casefold())
    return {token for token in tokens if len(token) >= 3 and token not in _TOKEN_STOP_WORDS}


def _uses_saved_taste(request: ConciergeRequest) -> bool:
    """Support the new personalised Explore option and legacy taste requests."""
    return bool(request.personalize_with_saved_taste or request.intent.value == "taste")


def _deterministic_score(
    work: Artwork,
    request: ConciergeRequest,
    qloo_artists: list[dict[str, Any]],
    direction: str,
    cultural_art_directions: list[str] | None = None,
) -> float:
    metadata_haystack = " ".join(
        part for part in [work.title, work.artist, work.medium, work.description] if part
    ).casefold()
    metadata_tokens = _tokenize(metadata_haystack)
    haystack = f"{metadata_haystack} {direction}".casefold()
    score = 0.0

    # Rank against the user's request itself, not only the query a provider happened
    # to receive. This is the central fit signal across Find, Curate, Learn and Buy.
    goal = _searchable_goal(request)
    goal_tokens = _tokenize(goal)
    goal_matches = goal_tokens & metadata_tokens
    score += min(30, len(goal_matches) * 4)
    title = (work.title or "").casefold().strip()
    normalized_goal = re.sub(r"[^a-z0-9]+", " ", goal.casefold()).strip()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    if normalized_goal and normalized_title and (normalized_goal in normalized_title or normalized_title in normalized_goal):
        score += 16
    # The optional culture-to-art bridge translates Qloo associations into art
    # catalogue terms. These terms only influence the score when they match
    # actual artwork metadata, not merely the provider query string.
    bridge_terms = set().union(*(_tokenize(value) for value in (cultural_art_directions or []))) if cultural_art_directions else set()
    bridge_matches = sum(1 for token in bridge_terms if token in metadata_haystack)
    score += min(12, bridge_matches * 2)
    if _uses_saved_taste(request):
        reference_text: list[str] = [request.profile_notes or "", *request.existing_ideas]
        for reference in request.existing_artworks:
            reference_text.extend([reference.title, reference.artist, reference.medium, reference.description])
        reference_tokens = _tokenize(" ".join(reference_text))
        reference_matches = reference_tokens & metadata_tokens
        score += min(24, len(reference_matches) * 5)
    qloo_names = {str(item.get("name") or "").strip().casefold() for item in qloo_artists if item.get("name")}
    avoid_names = {str(value).strip().casefold() for value in request.avoid_references if str(value).strip()}
    artist_name = work.artist.strip().casefold()
    if artist_name in avoid_names or any(value and value in artist_name for value in avoid_names):
        score -= 35
    # Do not award points for accidental substrings (e.g. a short culture name
    # appearing inside a different visual artist's name).
    if artist_name in qloo_names:
        score += 8
    request_terms = set()
    for value in request.art_interests + request.mediums + request.additional_interests:
        request_terms.update(_tokenize(value))
    score += min(24, 4 * sum(1 for token in request_terms if token in metadata_tokens))
    # Explicit medium and artistic-interest constraints deserve extra weight.
    medium_terms = set().union(*(_tokenize(value) for value in request.mediums)) if request.mediums else set()
    if medium_terms and medium_terms.issubset(metadata_tokens):
        score += 7
    if request.intent.value == "find":
        score += min(10, len(goal_matches) * 2)
    elif _uses_saved_taste(request):
        score += 4 if qloo_names else 0
    elif request.intent.value == "curate":
        score += 3 if work.medium else 0
    elif request.intent.value == "learn":
        score += 10 if work.source_kind == "institution" else 0
        score += 3 if work.description else 0
    if work.image_url:
        score += 4
    if _requires_purchasable_inventory(request):
        score += 35 if _is_purchasable(work) else -40
    if request.discovery_level >= 70:
        score += 7 if direction and not any(token in " ".join(request.art_interests).lower() for token in _tokenize(direction)) else 2
    elif request.discovery_level <= 30:
        score += 7 if any(token in haystack for token in request_terms) else 0
    else:
        score += 4
    if request.room and work.dimensions:
        score += 2
    return score


def _has_image_candidate(work: Artwork) -> bool:
    """Recommendations must carry a concrete image URL; page-only records are not enough."""
    return _usable_image_url(work.image_url)


def _is_purchasable(work: Artwork) -> bool:
    """True only for commercial works explicitly listed as available to acquire."""
    return work.source_kind == "commercial" and work.availability == "available"


def _requires_purchasable_inventory(request: ConciergeRequest) -> bool:
    """These services must return seller inventory, never institutional collection works."""
    return bool(request.purchase_required or request.personalize_with_saved_taste or request.intent.value in {"curate", "taste", "buy"})


def _taste_reference_directions(request: ConciergeRequest) -> list[str]:
    """Build search phrases from the user's own saved works, notes, and earlier ideas."""
    if not _uses_saved_taste(request):
        return []
    directions: list[str] = []
    notes = (request.profile_notes or "").strip()
    if notes:
        directions.append(notes[:140])
    directions.extend(str(idea).strip()[:160] for idea in request.existing_ideas if str(idea).strip())
    for work in request.existing_artworks[:6]:
        parts = [work.title, work.artist, work.medium]
        query = " ".join(str(part).strip() for part in parts if str(part).strip())
        if query:
            directions.append(query[:160])
        if work.description and (not query or _tokenize(work.description) - _tokenize(query)):
            directions.append(work.description[:120])
    return list(dict.fromkeys(value for value in directions if value))[:8]


def _provider_configured(provider: Any) -> bool:
    if provider.name == "Artsy":
        return bool(config.ARTSY_XAPP_TOKEN and config.ARTSY_PARTNER_ID)
    if provider.name == "Artlogic":
        return bool(config.ARTLOGIC_FEED_URL)
    if provider.name == "Collect24":
        return bool(config.COLLECT24_API_KEY)
    # Institutional collection APIs do not require application credentials.
    return True


def _safe_provider_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return f"HTTP {status_code}"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "Request timed out"
    return type(exc).__name__


async def _search_provider(provider: Any, direction: str, limit: int) -> tuple[str, str, list[Artwork], str | None]:
    try:
        works = await asyncio.wait_for(provider.search(direction, limit=limit), timeout=min(3.25, max(2.5, config.REQUEST_TIMEOUT * 0.82)))
        normalized = [
            replace(work, matched_direction=direction, image_url=_normalise_image_url(work.image_url))
            for work in works
        ]
        return provider.name, direction, normalized, None
    except Exception as exc:
        logger.exception("Artwork provider %s failed while searching direction %r", provider.name, direction)
        return provider.name, direction, [], _safe_provider_error(exc)


async def _research(
    directions: list[str],
    request: ConciergeRequest,
    status: list[dict[str, str]],
    *,
    include_extended_sources: bool = False,
    phase_label: str = "Researching artwork sources",
) -> tuple[list[Artwork], list[dict[str, Any]]]:
    """Search a bounded set of providers in parallel.

    Previous builds multiplied up to five directions by every provider, then did
    a second full fan-out and a second model review. This version makes one query
    per provider in each pass and only expands the source set when image-bearing
    results are insufficient.
    """
    progress = {"label": phase_label, "state": "active"}
    status.append(progress)
    commercial = commercial_providers()
    institutional = institutional_providers()
    if not include_extended_sources:
        institutional = [provider for provider in institutional if provider.name != "Rijksmuseum"]

    # Curate, personalised Explore, and Buy are purchase-oriented recommendations.
    # Institutional APIs are excluded so museum collection records cannot appear
    # as items the user could acquire.
    if _requires_purchasable_inventory(request):
        provider_groups = [commercial]
    elif request.intent.value == "learn":
        provider_groups = [institutional, commercial]
    else:
        provider_groups = [institutional, commercial]

    fallback_by_intent = {
        "buy": ["contemporary art for sale", "original contemporary painting"],
        "curate": ["contemporary painting for sale", "art photography available to buy"],
        "taste": ["art for sale inspired by your taste", "contemporary painting available to buy"],
        "learn": ["modern art", "art history"],
    }
    fallback = fallback_by_intent["taste"] if _uses_saved_taste(request) else fallback_by_intent.get(
        request.intent.value, ["contemporary art", "modern painting", "contemporary photography"]
    )
    proposed = list(dict.fromkeys([
        *[item for item in directions if isinstance(item, str) and item.strip()][:4],
        *request.art_interests[:2],
        *request.mediums[:1],
        *fallback,
    ]))
    search_directions = [item.strip() for item in proposed if item and item.strip()][:4]
    if not search_directions:
        search_directions = ["contemporary art"]

    works: list[Artwork] = []
    all_providers = list(dict.fromkeys(provider for group in provider_groups for provider in group))
    source_stats: dict[str, dict[str, Any]] = {}
    enabled_providers = []
    for provider in all_providers:
        configured = _provider_configured(provider)
        source_stats[provider.name] = {
            "provider": provider.name,
            "configured": configured,
            "queries_attempted": 0,
            "results_found": 0,
            "images_found": 0,
            "errors": [],
        }
        if configured:
            enabled_providers.append(provider)

    # One query per provider per pass. Rotate through the best search directions
    # to retain breadth without an N-providers × N-queries explosion.
    tasks = [
        _search_provider(provider, search_directions[index % len(search_directions)], limit=5)
        for index, provider in enumerate(enabled_providers)
    ]
    if tasks:
        responses = await asyncio.gather(*tasks)
        for provider_name, direction, batch, error in responses:
            stat = source_stats[provider_name]
            stat["queries_attempted"] += 1
            if error:
                if error not in stat["errors"]:
                    stat["errors"].append(error)
            else:
                stat["results_found"] += len(batch)
                stat["images_found"] += sum(1 for work in batch if _has_image_candidate(work))
                works.extend(batch)

    # Remove duplicates but retain image-less metadata long enough to diagnose
    # poor provider data. They are filtered before any visible result is chosen.
    deduped: dict[str, Artwork] = {}
    for work in works:
        if _budget_ok(work, request):
            deduped.setdefault(work.id, work)

    errors = [stat for stat in source_stats.values() if stat["errors"]]
    progress["state"] = "warning" if errors else "done"
    if errors:
        progress["detail"] = "Some artwork sources did not respond. Other sources were still searched."
        for stat in errors:
            status.append({
                "label": f"{stat['provider']} unavailable",
                "state": "warning",
                "detail": ", ".join(stat["errors"][:3]),
            })
    if not any(_has_image_candidate(work) for work in deduped.values()):
        logger.warning("No image-bearing artwork candidates found for queries %s; source diagnostics=%s", search_directions, source_stats)
    return list(deduped.values()), list(source_stats.values())


def _candidate_payload(work: Artwork) -> dict[str, Any]:
    return {
        "id": work.id,
        "title": work.title,
        "artist": work.artist,
        "medium": work.medium,
        "dimensions": work.dimensions,
        "year": work.year,
        "price": work.price,
        "currency": work.currency,
        "availability": work.availability,
        "source_kind": work.source_kind,
        "description": work.description,
        "matched_direction": work.matched_direction,
        "purchase_mode": work.purchase_mode,
        "seller_name": work.seller_name,
        "checkout_available": bool(checkout_catalog_entry(work.id)),
    }


def _review_map(review: CandidateReview | None) -> dict[str, Any]:
    if review is None:
        return {"assessments": {}, "follow_up_queries": [], "critique": ""}
    assessments = {
        item.artwork_id: {
            "score": item.score,
            "fit_reason": item.fit_reason,
            "concern": item.concern,
        }
        for item in review.assessments
    }
    return {
        "assessments": assessments,
        "follow_up_queries": review.follow_up_queries,
        "critique": review.critique,
        "coverage": review.coverage,
    }


def _select(
    works: list[Artwork],
    request: ConciergeRequest,
    qloo_artists: list[dict[str, Any]],
    review: CandidateReview | None,
    cultural_art_directions: list[str] | None = None,
) -> list[Artwork]:
    review_by_id = {item.artwork_id: item for item in (review.assessments if review else [])}
    excluded_ids = {str(value).strip() for value in request.exclude_artwork_ids if str(value).strip()}
    avoided_artists = {
        re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
        for value in request.avoid_references if str(value).strip()
    }

    def excluded(work: Artwork) -> bool:
        if str(work.id).strip() in excluded_ids:
            return True
        artist = re.sub(r"[^a-z0-9]+", " ", str(work.artist or "").casefold()).strip()
        if artist in {"unknown artist", "anonymous artist", "various artists", ""}:
            return False
        return any(reference and (artist == reference or f" {reference} " in f" {artist} ") for reference in avoided_artists)

    ranked = sorted(
        (work for work in works if _has_image_candidate(work) and not excluded(work)),
        key=lambda work: (
            1 if _usable_image_url(work.image_url) else 0,
            review_by_id.get(work.id).score if work.id in review_by_id else _deterministic_score(work, request, qloo_artists, work.matched_direction or "", cultural_art_directions),
            _deterministic_score(work, request, qloo_artists, work.matched_direction or "", cultural_art_directions),
        ),
        reverse=True,
    )

    seen_artists: set[str] = set()
    selected: list[Artwork] = []
    target = min(max(1, request.number_of_works), 8)
    for work in ranked:
        artist_key = work.artist.strip().lower()
        # Curate prefers artist variety; other intents can return more than one work.
        if request.intent.value == "curate" and artist_key and artist_key in seen_artists and len(selected) < target:
            continue
        if _requires_purchasable_inventory(request) and not _is_purchasable(work):
            continue
        selected.append(work)
        if artist_key:
            seen_artists.add(artist_key)
        if len(selected) >= (target if request.intent.value in {"curate", "buy"} else 10):
            break
    return selected



async def _qloo_context(
    request: ConciergeRequest,
    signals: RequestSignals,
    status: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve known references and expand them through Qloo as a best-effort task."""
    resolved = await _resolve_entities(request, status, signals)
    artists = await _qloo_taste(resolved, request, status) if resolved else []
    return resolved, artists


_GENERIC_SERVICE_PROMPTS = {
    "develop my taste", "i want to develop my taste", "help me develop my taste",
    "develop my taste in art", "discover", "discover art", "find", "find art",
    "curate", "curate art", "buy", "buy art", "learn", "learn about art",
    "tell me what you are looking for", "describe what you are looking for",
}


def _searchable_goal(request: ConciergeRequest) -> str:
    """Exclude service labels so they do not masquerade as artwork search terms."""
    goal = (request.goal or "").strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", goal.casefold()).strip()
    return "" if normalized in _GENERIC_SERVICE_PROMPTS else goal[:140]


def _fast_search_directions(request: ConciergeRequest) -> list[str]:
    """Build catalogue-friendly queries without promoting broad culture inputs into art terms."""
    fallback_by_intent = {
        "buy": ["contemporary art for sale", "original contemporary painting"],
        "curate": ["contemporary painting", "art photography"],
        "taste": ["contemporary painting", "abstract art"],
        "learn": ["modern art", "art history"],
    }
    if _uses_saved_taste(request):
        fallback = fallback_by_intent["taste"]
    else:
        fallback = fallback_by_intent.get(
            request.intent.value,
            ["contemporary art", "modern painting", "contemporary photography"],
        )
    explicit = [
        _searchable_goal(request),
        *request.art_interests[:2],
        *request.mediums[:1],
        *fallback,
    ]
    return list(dict.fromkeys(item for item in explicit if item))[:4]


_LEARNING_TOPICS: dict[str, dict[str, Any]] = {
    "abstract expressionism": {
        "overview": "Abstract Expressionism emerged in New York in the 1940s and 1950s. Artists used abstraction, scale, gesture and colour to make the act of painting itself central; approaches ranged from energetic marks to large fields of colour.",
        "key_ideas": ["Gesture and process can be visible in the finished surface.", "Scale and the viewer's physical relationship to a work affect how it is experienced.", "Abstraction can communicate through colour, rhythm and composition without depicting a literal scene."],
        "look_closer": ["Follow the direction and speed implied by brushwork or marks.", "Compare dense, active surfaces with quieter areas.", "Notice whether colour creates depth, tension or a sense of stillness."],
        "terms": [{"term": "Gestural", "definition": "Mark-making that makes an artist's physical movement apparent."}, {"term": "Nonrepresentational", "definition": "Not depicting recognisable objects or scenes."}, {"term": "Colour field", "definition": "A broad area of colour that carries much of a work's visual effect."}],
    },
    "impressionism": {
        "overview": "Impressionism developed in France in the late nineteenth century. It is associated with attention to changing light, atmosphere and modern life, often conveyed through visible, broken brushstrokes and colour placed side by side.",
        "key_ideas": ["Light and atmosphere can matter more than sharply outlined detail.", "Visible brushwork keeps the viewer aware of how the image was made.", "Everyday scenes and outdoor settings became important subjects."],
        "look_closer": ["Step back to see how separate marks blend optically.", "Compare warm and cool colour in areas of light and shadow.", "Look for the moment or atmosphere the artist chose to capture."],
        "terms": [{"term": "Broken brushwork", "definition": "Short or distinct brushstrokes that remain visible."}, {"term": "En plein air", "definition": "Painting outdoors, often directly from the subject."}, {"term": "Atmosphere", "definition": "The feeling of air, light or weather conveyed by a scene."}],
    },
    "cubism": {
        "overview": "Cubism developed in the early twentieth century, especially through the work of Pablo Picasso and Georges Braque. Rather than treating a picture as a single window onto the world, Cubist artists fragmented forms and explored multiple viewpoints within one composition.",
        "key_ideas": ["Objects may be broken into geometric planes.", "The picture surface can flatten the distinction between foreground and background.", "A subject can be suggested through several partial views instead of one fixed viewpoint."],
        "look_closer": ["Identify the shapes into which a recognisable subject has been divided.", "Notice where an object and its background seem to merge.", "Look for clues that help you reconstruct the subject."],
        "terms": [{"term": "Facet", "definition": "A flat plane or surface within a fragmented form."}, {"term": "Viewpoint", "definition": "The position or angle from which a subject is seen."}, {"term": "Still life", "definition": "An artwork centred on arranged, usually inanimate objects."}],
    },
    "surrealism": {
        "overview": "Surrealism took shape in Europe in the 1920s and explored dreams, unexpected associations and the unsettling combination of familiar things. Surrealist works can look precise while presenting scenes that defy ordinary logic.",
        "key_ideas": ["Juxtaposition can create meaning or unease.", "Realistic rendering does not necessarily mean a scene is realistic.", "Ambiguity invites multiple interpretations rather than a single literal answer."],
        "look_closer": ["List the objects or figures, then ask what feels unexpected about their relationship.", "Notice shifts in scale, setting or logic.", "Separate what is visibly present from what you infer it might mean."],
        "terms": [{"term": "Juxtaposition", "definition": "Placing elements together so their relationship becomes noticeable."}, {"term": "Dream imagery", "definition": "Images that evoke dreamlike or illogical situations."}, {"term": "Ambiguity", "definition": "A quality that supports more than one plausible reading."}],
    },
    "minimalism": {
        "overview": "Minimalism, especially prominent in the 1960s, reduces visual or sculptural elements to simple forms, repeated units and restrained materials. The viewer's experience of space, scale and the object itself often becomes as important as symbolism.",
        "key_ideas": ["Simple forms can make proportion and placement more noticeable.", "Repetition and serial structure can change how a work is read.", "The surrounding room and the viewer's movement may be part of the experience."],
        "look_closer": ["Compare the proportions and spacing of repeated elements.", "Notice surface, material and edges rather than searching immediately for a hidden story.", "Observe how the work occupies or changes the space around it."],
        "terms": [{"term": "Seriality", "definition": "Using repeated or systematically varied forms."}, {"term": "Scale", "definition": "The size of a work in relation to the viewer or its setting."}, {"term": "Negative space", "definition": "The empty space around or within forms."}],
    },
    "pop art": {
        "overview": "Pop Art developed in the 1950s and 1960s and drew on mass media, advertising, consumer products and popular culture. It often blurs boundaries between fine art and images encountered in everyday life.",
        "key_ideas": ["Borrowed imagery can change meaning when placed in an art context.", "Repetition can make an image feel familiar, mechanical or newly strange.", "Bright colour and commercial-looking techniques may both attract and critique attention."],
        "look_closer": ["Ask where the image may have appeared before entering the artwork.", "Notice cropping, repetition, scale and colour choices.", "Consider whether the work celebrates, questions or simply re-presents consumer culture."],
        "terms": [{"term": "Appropriation", "definition": "Reusing existing images or visual material in a new context."}, {"term": "Mass media", "definition": "Media produced for broad public audiences."}, {"term": "Repetition", "definition": "Repeating an image or element to alter its visual effect or meaning."}],
    },
    "renaissance": {
        "overview": "The Renaissance describes a broad period of renewed engagement with classical learning and major developments in European art, beginning in Italy around the fourteenth century and spreading over subsequent centuries. Many works combine careful observation, perspective, anatomy and religious or civic subjects, though practices varied by place and time.",
        "key_ideas": ["Linear perspective can organise pictorial space around a viewpoint.", "Human figures, anatomy and natural observation received sustained attention.", "Religious, civic and patronage contexts help explain why works were made."],
        "look_closer": ["Look for lines or architectural features that lead into depth.", "Notice how figures are posed and how their gestures direct attention.", "Use the work's date, location and collection record before making contextual claims."],
        "terms": [{"term": "Linear perspective", "definition": "A system for suggesting depth using converging lines."}, {"term": "Patronage", "definition": "Financial or institutional support that enables a work to be made."}, {"term": "Composition", "definition": "The arrangement of elements in an artwork."}],
    },
    "contemporary art": {
        "overview": "Contemporary art refers broadly to art made in the present era, including painting, photography, sculpture, installation, video and practices that cross media. It is not one single style; ideas, materials, context and the way a work meets its audience can all be central.",
        "key_ideas": ["Art can take many forms beyond a framed painting.", "Materials and display choices shape how a work is experienced.", "Historical and social context can be important, but should be grounded in reliable source information."],
        "look_closer": ["Start with what you can directly see or hear before interpreting it.", "Notice material, scale, display and the relation between the work and its setting.", "Check the source record for date, medium and provenance details."],
        "terms": [{"term": "Installation", "definition": "A work arranged for a particular space or viewing experience."}, {"term": "Medium", "definition": "The material or method used to make a work."}, {"term": "Provenance", "definition": "The documented history of an artwork's ownership."}],
    },
    "claude monet": {
        "overview": "Claude Monet was a French painter closely associated with Impressionism. His work often investigates changing daylight, atmosphere and the effects of colour, including through repeated studies of similar subjects under different conditions.",
        "key_ideas": ["Light and weather can change how the same subject appears.", "Visible brushstrokes can be part of the image's structure.", "Series of related paintings can make time and changing conditions part of the subject."],
        "look_closer": ["Compare colour and edges in areas of sun, shadow or reflection.", "Step back and then move closer to see how marks combine.", "When comparing related works, note what changes and what stays constant."],
        "terms": [{"term": "Impressionism", "definition": "A movement attentive to light, atmosphere and visible brushwork."}, {"term": "Series", "definition": "Related works that explore one subject or problem across several versions."}, {"term": "Optical mixing", "definition": "Colours appearing to blend when separate marks are viewed together."}],
    },
    "monet": {
        "overview": "Claude Monet was a French painter closely associated with Impressionism. His work often investigates changing daylight, atmosphere and the effects of colour, including through repeated studies of similar subjects under different conditions.",
        "key_ideas": ["Light and weather can change how the same subject appears.", "Visible brushstrokes can be part of the image's structure.", "Series of related paintings can make time and changing conditions part of the subject."],
        "look_closer": ["Compare colour and edges in areas of sun, shadow or reflection.", "Step back and then move closer to see how marks combine.", "When comparing related works, note what changes and what stays constant."],
        "terms": [{"term": "Impressionism", "definition": "A movement attentive to light, atmosphere and visible brushwork."}, {"term": "Series", "definition": "Related works that explore one subject or problem across several versions."}, {"term": "Optical mixing", "definition": "Colours appearing to blend when separate marks are viewed together."}],
    },
    "vincent van gogh": {
        "overview": "Vincent van Gogh was a Dutch painter associated with Post-Impressionism. His paintings are often discussed through their expressive brushwork, strong colour relationships and energetic handling of landscape, still life and portrait subjects.",
        "key_ideas": ["Brushstroke direction can create rhythm and movement.", "Colour may express structure or mood rather than simply copy local colour.", "Post-Impressionism describes varied approaches, not one uniform style."],
        "look_closer": ["Follow how brushstrokes move across the surface.", "Compare colour contrasts and their effect on focal points.", "Use dates and source records when comparing works from different periods."],
        "terms": [{"term": "Post-Impressionism", "definition": "A broad label for artists who developed different responses to Impressionism."}, {"term": "Complementary colours", "definition": "Colours opposite one another on a traditional colour wheel."}, {"term": "Impasto", "definition": "Paint applied thickly so the surface has visible texture."}],
    },
    "van gogh": {
        "overview": "Vincent van Gogh was a Dutch painter associated with Post-Impressionism. His paintings are often discussed through their expressive brushwork, strong colour relationships and energetic handling of landscape, still life and portrait subjects.",
        "key_ideas": ["Brushstroke direction can create rhythm and movement.", "Colour may express structure or mood rather than simply copy local colour.", "Post-Impressionism describes varied approaches, not one uniform style."],
        "look_closer": ["Follow how brushstrokes move across the surface.", "Compare colour contrasts and their effect on focal points.", "Use dates and source records when comparing works from different periods."],
        "terms": [{"term": "Post-Impressionism", "definition": "A broad label for artists who developed different responses to Impressionism."}, {"term": "Complementary colours", "definition": "Colours opposite one another on a traditional colour wheel."}, {"term": "Impasto", "definition": "Paint applied thickly so the surface has visible texture."}],
    },
    "pablo picasso": {
        "overview": "Pablo Picasso was a Spanish artist whose long career moved through several styles and media. He was a key figure in the development of Cubism, which fragmented forms and challenged a single fixed viewpoint, but Cubism is only one part of his practice.",
        "key_ideas": ["One artist's work can change substantially over time.", "Cubist compositions can combine partial views and geometric planes.", "A date and medium help place a particular work within a large body of work."],
        "look_closer": ["Identify which parts of a subject remain recognisable.", "Notice where different viewpoints seem to overlap.", "Compare works by date rather than assuming every work uses the same style."],
        "terms": [{"term": "Cubism", "definition": "An early twentieth-century approach that fragmented forms and explored multiple viewpoints."}, {"term": "Analytical Cubism", "definition": "An early Cubist phase often marked by fragmented forms and a restrained palette."}, {"term": "Collage", "definition": "A composition built by attaching or combining different materials."}],
    },
    "picasso": {
        "overview": "Pablo Picasso was a Spanish artist whose long career moved through several styles and media. He was a key figure in the development of Cubism, which fragmented forms and challenged a single fixed viewpoint, but Cubism is only one part of his practice.",
        "key_ideas": ["One artist's work can change substantially over time.", "Cubist compositions can combine partial views and geometric planes.", "A date and medium help place a particular work within a large body of work."],
        "look_closer": ["Identify which parts of a subject remain recognisable.", "Notice where different viewpoints seem to overlap.", "Compare works by date rather than assuming every work uses the same style."],
        "terms": [{"term": "Cubism", "definition": "An early twentieth-century approach that fragmented forms and explored multiple viewpoints."}, {"term": "Analytical Cubism", "definition": "An early Cubist phase often marked by fragmented forms and a restrained palette."}, {"term": "Collage", "definition": "A composition built by attaching or combining different materials."}],
    },
    "frida kahlo": {
        "overview": "Frida Kahlo was a Mexican painter widely known for self-portraits and works that combine recognisable imagery with symbolism. To interpret a particular painting carefully, distinguish visible motifs from biographical claims and check the source record for its date and context.",
        "key_ideas": ["Portraits can communicate through pose, setting, objects and direct gaze.", "Symbolic details invite interpretation but should be tied to visual evidence.", "Personal and cultural context can matter, but one detail rarely settles the meaning of a work."],
        "look_closer": ["Describe the pose, surrounding objects and background before interpreting them.", "Ask which visual clues support a reading of the symbols.", "Compare a source's account of the painting with what is visibly present."],
        "terms": [{"term": "Self-portrait", "definition": "An artwork in which the artist depicts themself."}, {"term": "Symbolism", "definition": "Using visual elements to suggest ideas beyond their literal appearance."}, {"term": "Iconography", "definition": "The study of recurring images and symbols in artworks."}],
    },
    "rembrandt": {
        "overview": "Rembrandt van Rijn was a Dutch painter and printmaker of the seventeenth-century Dutch Golden Age. His work is often studied for its portraiture, use of light and shadow, expressive faces and the range of subjects he handled across painting and printmaking.",
        "key_ideas": ["Light and shadow can direct attention and suggest volume.", "Portraits can convey character through gaze, pose, clothing and lighting.", "Prints and paintings use different techniques to organise tone and detail."],
        "look_closer": ["Notice where light falls and what remains in shadow.", "Look at the subject's expression, hands and pose as well as the face.", "Check whether the source record describes a painting, drawing or print."],
        "terms": [{"term": "Chiaroscuro", "definition": "The use of strong light-dark contrast to model forms."}, {"term": "Etching", "definition": "A printmaking process that uses acid to incise lines into a metal plate."}, {"term": "Portraiture", "definition": "Art depicting an individual or group."}],
    },
    "yayoi kusama": {
        "overview": "Yayoi Kusama is a Japanese contemporary artist known for work across painting, sculpture, installation and other media. Repetition, dots, pattern and immersive environments recur in her practice, though any one work needs to be considered on its own terms.",
        "key_ideas": ["Repeated marks can create rhythm and visual intensity.", "Mirrors and installation can make the viewer part of the experience.", "A recurring motif can behave differently across painting, sculpture and space."],
        "look_closer": ["Count repeated forms and notice how their spacing changes.", "Notice whether the viewer sees the work from outside, inside or through reflection.", "Compare the same visual motif across different media."],
        "terms": [{"term": "Installation", "definition": "A work arranged for a particular space and viewing experience."}, {"term": "Repetition", "definition": "The reuse of a visual element to establish a pattern or rhythm."}, {"term": "Immersive", "definition": "An environment that surrounds or deeply involves the viewer."}],
    },
    "georgia o keeffe": {
        "overview": "Georgia O'Keeffe was an American modernist painter. Her work includes close-up flower paintings, landscapes and studies of forms that move between recognisable subjects and abstraction.",
        "key_ideas": ["Cropping and scale can make familiar subjects feel unfamiliar.", "Large shapes can shift attention from object identity to form and colour.", "A painting can sit between observation and abstraction without being wholly nonrepresentational."],
        "look_closer": ["Notice how closely the subject is cropped.", "Compare outlines, colour transitions and the amount of surrounding space.", "Ask how the scale changes your attention to the subject."],
        "terms": [{"term": "Cropping", "definition": "Cutting off part of a subject or frame to change emphasis."}, {"term": "Abstraction", "definition": "Simplifying or transforming forms rather than depicting them literally."}, {"term": "Scale", "definition": "Size in relation to the viewer, object or setting."}],
    },
    "jackson pollock": {
        "overview": "Jackson Pollock was an American painter associated with Abstract Expressionism. He is known for poured and dripped paint and for compositions in which lines and marks spread across much of the canvas rather than forming one conventional central scene.",
        "key_ideas": ["The trace of a physical gesture can become the subject of a painting.", "An all-over composition distributes attention across the surface.", "Process and layering can be important alongside colour and line."],
        "look_closer": ["Follow the paths and intersections of dripped or poured paint.", "Notice whether any region dominates or the surface feels evenly active.", "Look for layering, density and shifts in rhythm."],
        "terms": [{"term": "All-over composition", "definition": "An arrangement where visual activity extends across the picture plane."}, {"term": "Gesture", "definition": "A mark that records a physical movement or action."}, {"term": "Layering", "definition": "Building an image through successive applications of material."}],
    },
    "mark rothko": {
        "overview": "Mark Rothko was an American painter associated with the colour-field branch of Abstract Expressionism. Many of his mature paintings use broad, soft-edged areas of colour, inviting attention to scale, colour relationships and the viewer's sustained experience of the surface.",
        "key_ideas": ["Large areas of colour can create relationships without depicting objects.", "Soft edges and overlapping tones affect how colours appear to sit in space.", "Viewing distance and time can change the experience of a large painting."],
        "look_closer": ["Compare the edges and relative sizes of the colour fields.", "Notice how one colour changes when placed beside another.", "Take time with the surface before deciding what emotion it suggests."],
        "terms": [{"term": "Colour field", "definition": "A broad area of colour central to a work's visual structure."}, {"term": "Edge", "definition": "The boundary between forms or areas of colour."}, {"term": "Tonal relationship", "definition": "How lightness, darkness and colour interact in an image."}],
    },
    "andy warhol": {
        "overview": "Andy Warhol was an American artist associated with Pop Art. He used imagery from celebrity culture, advertising and consumer goods, and often employed repetition and commercial image-making processes to question how popular images circulate.",
        "key_ideas": ["Familiar images can change meaning when repeated or enlarged.", "Mechanical-looking reproduction can complicate ideas of originality.", "A work can both use and question the visual language of advertising."],
        "look_closer": ["Identify what image is being repeated or borrowed.", "Compare shifts in colour, crop and scale between versions.", "Ask how the work changes an image you already recognise."],
        "terms": [{"term": "Pop Art", "definition": "Art that engages with mass culture, advertising and everyday imagery."}, {"term": "Screenprinting", "definition": "A printing method that pushes ink through a mesh stencil."}, {"term": "Appropriation", "definition": "Reusing an existing image in a new context."}],
    },
}

def _learning_topic_label(goal: str | None) -> str:
    raw = re.sub(r"\s+", " ", (goal or "Art history")).strip(" .?!")
    raw = re.sub(r"^(?:please\s+)?(?:explain|teach me about|tell me about|help me understand|who is|who was|what is|what are|learn about|show me|describe)\s+", "", raw, flags=re.I)
    raw = re.sub(r"^(?:the basics of|an introduction to)\s+", "", raw, flags=re.I)
    return raw[:120].strip(" .?!") or "Art history"

def _build_learning_guide(goal: str | None, works: list[Artwork]) -> dict[str, Any]:
    topic = _learning_topic_label(goal)
    normalized = re.sub(r"[^a-z0-9]+", " ", topic.casefold()).strip()
    profile = next((value for key, value in _LEARNING_TOPICS.items() if key in normalized), None)
    if profile is None:
        profile = {
            "overview": f"Use “{topic}” as a question to investigate rather than a label to memorise. Begin with the visible evidence in each work, compare how artists make choices, and use the collection records to confirm dates, materials and context. This guide avoids assuming a single interpretation when the topic is broad or the source records are incomplete.",
            "key_ideas": ["Separate observable details from your interpretation of what they mean.", "Compare composition, material, colour, scale and subject across different works.", "Use dates, artist details and collection descriptions to ground historical claims."],
            "look_closer": ["Describe three visible details before deciding what a work means.", "Compare one formal choice across two works, such as colour, scale or material.", "Open the source record to check the work's title, date, medium and any available context."],
            "terms": [{"term": "Context", "definition": "The historical, social and display circumstances around a work."}, {"term": "Formal analysis", "definition": "Looking closely at composition, colour, line, space, material and technique."}, {"term": "Interpretation", "definition": "An explanation built from visual evidence and context, rather than guesswork."}],
        }
    return {
        "topic": topic,
        "overview": profile["overview"],
        "key_ideas": list(profile["key_ideas"]),
        "look_closer": list(profile["look_closer"]),
        "terms": list(profile["terms"]),
        "questions": [
            "Which details can I point to in the work to support my interpretation?",
            "What changes when I compare this work with another example?",
            "Which claims need confirmation from the artist, museum or collection record?",
        ],
        "related_works": [{
            "id": work.id, "title": work.title, "artist": work.artist,
            "medium": work.medium, "year": work.year, "source": work.source,
            "detail_url": work.detail_url,
        } for work in works[:3]],
        "source_note": "Use each collection or seller record to verify artwork-specific dates, materials and provenance. The examples below are works returned by this search, not a claim that every one belongs to the named movement.",
    }


async def run_concierge(request: ConciergeRequest) -> dict[str, Any]:
    """Search the written brief first, then require an image-led second pass when images are attached.

    Text-only requests retain the short response target. Requests with images use a
    longer API deadline because image interpretation and a second catalogue pass are
    required work, not optional enrichment.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    has_images = bool(request.inspiration_images)
    response_target_ms = 17500 if has_images else 4500
    status: list[dict[str, str]] = [{"label": "Request received", "state": "done"}]

    # Learn about art produces an educational guide only. It does not run catalogue
    # searches or surface artwork recommendations. When an inspiration image is
    # attached, image interpretation is required and is folded into the guide.
    if request.intent.value == "learn":
        guide = _build_learning_guide(request.goal, [])
        visual_analysis = None
        if has_images:
            if not config.GEMINI_API_KEY:
                detail = "ArtCo could not interpret the attached image because image analysis is not configured. No guide was returned without using the image. Configure Gemini and retry, or remove the image to request a text-only guide."
                return {
                    "success": False, "error_type": "RequiredVisualSearchFailed", "detail": detail,
                    "diagnostics": {"duration_ms": round((loop.time() - started) * 1000), "response_target_ms": response_target_ms, "gemini_configured": False, "text_guide_generated": True},
                    "visual_inspiration": {"image_count": len(request.inspiration_images), "analyzed": False, "message": detail},
                    "results": [], "not_for_sale": [], "status": status,
                }
            try:
                visual_analysis = await asyncio.wait_for(
                    analyze_inspiration_images(request.goal or "", request.inspiration_images), timeout=8.5
                )
            except Exception as exc:
                logger.warning("Required learning-image interpretation failed (%s)", type(exc).__name__)
                visual_analysis = None
            if visual_analysis is None:
                detail = "ArtCo prepared the learning topic but could not interpret the attached image. No image-independent guide was returned. Check the image-analysis configuration and retry, or remove the image to request a text-only guide."
                return {
                    "success": False, "error_type": "RequiredVisualSearchFailed", "detail": detail,
                    "diagnostics": {"duration_ms": round((loop.time() - started) * 1000), "response_target_ms": response_target_ms, "gemini_configured": bool(config.GEMINI_API_KEY), "gemini_model": config.GEMINI_MODEL, "text_guide_generated": True},
                    "visual_inspiration": {"image_count": len(request.inspiration_images), "analyzed": False, "message": detail},
                    "results": [], "not_for_sale": [], "status": status,
                }
            visual_note = visual_analysis.summary.strip()
            if visual_note:
                guide["overview"] += f" Your attached image offers a visual point of comparison: {visual_note}"
            image_traits = list(dict.fromkeys([*visual_analysis.visual_attributes, *visual_analysis.colour_palette]))
            if image_traits:
                guide["look_closer"] = [f"Compare the image's {trait} with the ideas described above." for trait in image_traits[:3]] + guide["look_closer"][:2]
            guide["source_note"] = "The attached image was interpreted as part of this guide. The visual description is a starting point for observation, not a verified identification or provenance record."
            status.append({"label": "Attached image incorporated into the learning guide", "state": "done"})
        status.append({"label": "Learning guide generated", "state": "done"})
        elapsed = round((loop.time() - started) * 1000)
        return {
            "success": True,
            "intent": "learn",
            "summary": f"A learning guide to {guide['topic']}.",
            "learning_guide": guide,
            "results": [],
            "not_for_sale": [],
            "purchase_available": 0,
            "total_found": 0,
            "diagnostics": {
                "duration_ms": elapsed, "response_target_ms": response_target_ms,
                "gemini_configured": bool(config.GEMINI_API_KEY), "gemini_model": config.GEMINI_MODEL,
                "text_guide_generated": True, "catalogue_search_performed": False,
                "image_analysis_required": has_images, "image_analysis_performed": visual_analysis is not None,
            },
            "visual_inspiration": {
                "image_count": len(request.inspiration_images), "analyzed": visual_analysis is not None,
                "summary": visual_analysis.summary if visual_analysis else "",
                "visual_attributes": visual_analysis.visual_attributes if visual_analysis else [],
                "colour_palette": visual_analysis.colour_palette if visual_analysis else [],
                "search_terms": visual_analysis.search_terms if visual_analysis else [],
                "message": "Your image was incorporated into the learning guide." if visual_analysis else "",
            },
            "status": status,
        }

    fallback_signals = RequestSignals(
        cultural_references=list(dict.fromkeys(request.loves + request.additional_interests))[:10],
        art_interests=request.art_interests[:8],
        mediums=request.mediums[:6],
        room=request.room,
        size_preference=request.size_preference,
        market=request.preferred_market,
        budget_min=request.budget_min,
        budget_max=request.budget_max,
    )

    taste_reference_directions = _taste_reference_directions(request)
    signal_interests = list(dict.fromkeys([
        *request.additional_interests,
        *request.art_interests,
        *taste_reference_directions,
    ]))[:16]
    if request.profile_notes and _uses_saved_taste(request):
        signal_interests = list(dict.fromkeys([*signal_interests, request.profile_notes.strip()[:180]]))[:16]
    signals_task = asyncio.create_task(extract_request_signals(
        request.goal or "",
        request.loves,
        signal_interests,
        request.avoid_references,
    ))

    async def qloo_after_signal_extraction() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Resolve taste references independently while the written search runs.
        try:
            qloo_signals = await asyncio.wait_for(asyncio.shield(signals_task), timeout=0.9)
            if not isinstance(qloo_signals, RequestSignals):
                raw_signals = qloo_signals.model_dump() if hasattr(qloo_signals, "model_dump") else vars(qloo_signals)
                qloo_signals = RequestSignals.model_validate(raw_signals)
        except Exception:
            qloo_signals = fallback_signals
        return await _qloo_context(request, qloo_signals, status)

    qloo_task = asyncio.create_task(qloo_after_signal_extraction())

    async def qloo_art_direction_bridge() -> list[str]:
        """Translate Qloo cultural associations into art terms without blocking results."""
        try:
            resolved_items, related_artists = await asyncio.wait_for(asyncio.shield(qloo_task), timeout=2.8)
            if not resolved_items or not related_artists:
                return []
            planner_signals = fallback_signals
            if signals_task.done() and not signals_task.cancelled() and signals_task.exception() is None:
                candidate_signals = signals_task.result()
                try:
                    planner_signals = candidate_signals if isinstance(candidate_signals, RequestSignals) else RequestSignals.model_validate(
                        candidate_signals.model_dump() if hasattr(candidate_signals, "model_dump") else candidate_signals
                    )
                except Exception:
                    planner_signals = fallback_signals
            return await _build_search_plan(request, planner_signals, related_artists, status)
        except Exception as exc:
            logger.debug("Optional Qloo-to-art translation skipped (%s)", type(exc).__name__)
            return []

    qloo_direction_task = asyncio.create_task(qloo_art_direction_bridge())
    optional_tasks = [signals_task, qloo_task, qloo_direction_task]

    async def stop_optional_tasks() -> None:
        pending_optional = [task for task in optional_tasks if not task.done()]
        for task in pending_optional:
            task.cancel()
        if pending_optional:
            await asyncio.gather(*pending_optional, return_exceptions=True)

    # Phase 1: the written-request search always runs first. Image bytes are not
    # sent to the vision model until this catalogue pass has completed. Fast text
    # interpretation may enrich this first text pass, but image-derived terms cannot.
    await asyncio.wait([signals_task], timeout=0.55)
    early_signals = fallback_signals
    if signals_task.done() and not signals_task.cancelled() and signals_task.exception() is None:
        candidate_signals = signals_task.result()
        try:
            early_signals = candidate_signals if isinstance(candidate_signals, RequestSignals) else RequestSignals.model_validate(
                candidate_signals.model_dump() if hasattr(candidate_signals, "model_dump") else candidate_signals
            )
        except Exception:
            early_signals = fallback_signals
    fast_text_directions = _fast_search_directions(request)
    search_goal = _searchable_goal(request)
    # For personalised Explore, reserve catalogue queries for saved works and ideas
    # before generic fallbacks so the recommendations actually build on them.
    if _uses_saved_taste(request):
        text_directions = list(dict.fromkeys([
            search_goal,
            *taste_reference_directions[:2],
            *early_signals.art_interests[:1],
            *request.art_interests[:1],
            *request.mediums[:1],
            *[item for item in fast_text_directions if item != search_goal],
        ]))[:4]
    else:
        text_directions = list(dict.fromkeys([
            search_goal,
            *early_signals.art_interests[:2],
            *request.art_interests[:2],
            *request.mediums[:1],
            *[item for item in fast_text_directions if item != search_goal],
        ]))[:4]
    text_directions = [item for item in text_directions if item]
    text_works, text_source_diagnostics = await _research(
        text_directions,
        request,
        status,
        phase_label="Searching your written request",
    )
    status.append({
        "label": "Written-request search complete",
        "state": "done",
        "detail": f"Searched the written brief and found {len(text_works)} catalogue records before image analysis.",
    })

    visual_analysis = None
    visual_directions: list[str] = []
    visual_works: list[Artwork] = []
    visual_source_diagnostics: list[dict[str, Any]] = []
    visual_search_performed = False

    if has_images:
        vision_progress = {"label": "Interpreting your inspiration images", "state": "active"}
        status.append(vision_progress)

        # Image use is mandatory when images were attached. Do not silently treat a
        # missing key, provider failure, or timeout as permission to return text-only results.
        if not config.GEMINI_API_KEY:
            vision_progress.update({"state": "warning", "detail": "Gemini image analysis is not configured."})
            await stop_optional_tasks()
            detail = (
                "ArtCo completed the written-request search, but an image was attached and image analysis is required. "
                "Gemini is not configured, so ArtCo has not returned text-only recommendations. Add GEMINI_API_KEY and retry, "
                "or remove the image to run a text-only search."
            )
            return {
                "success": False,
                "error_type": "RequiredVisualSearchFailed",
                "detail": detail,
                "diagnostics": {
                    "duration_ms": round((loop.time() - started) * 1000),
                    "response_target_ms": response_target_ms,
                    "gemini_configured": False,
                    "text_search_completed": True,
                    "text_search_results_found": len(text_works),
                    "text_source_diagnostics": text_source_diagnostics,
                },
                "visual_inspiration": {
                    "image_count": len(request.inspiration_images),
                    "analyzed": False,
                    "message": detail,
                },
                "results": [],
                "not_for_sale": [],
                "status": status,
            }

        try:
            # Deliberately after the full text search. The image-attached endpoint
            # allows up to 17.5 seconds for text search + vision + visual search.
            visual_analysis = await asyncio.wait_for(
                analyze_inspiration_images(request.goal or "", request.inspiration_images),
                timeout=8.5,
            )
        except asyncio.TimeoutError:
            logger.warning("Required inspiration-image analysis exceeded its 8.5-second phase limit")
            visual_analysis = None
        except Exception as exc:
            logger.warning("Required inspiration-image analysis failed (%s)", type(exc).__name__)
            visual_analysis = None

        if visual_analysis is None:
            vision_progress.update({"state": "warning", "detail": "Image analysis failed or timed out."})
            await stop_optional_tasks()
            detail = (
                "ArtCo completed the written-request search, but could not interpret the attached image. "
                "Because an image was attached, ArtCo has not returned text-only recommendations. Check Gemini's API key and model configuration, "
                "then retry, or remove the image to run a text-only search."
            )
            return {
                "success": False,
                "error_type": "RequiredVisualSearchFailed",
                "detail": detail,
                "diagnostics": {
                    "duration_ms": round((loop.time() - started) * 1000),
                    "response_target_ms": response_target_ms,
                    "gemini_configured": bool(config.GEMINI_API_KEY),
                    "gemini_model": config.GEMINI_MODEL,
                    "text_search_completed": True,
                    "text_search_results_found": len(text_works),
                    "text_source_diagnostics": text_source_diagnostics,
                },
                "visual_inspiration": {
                    "image_count": len(request.inspiration_images),
                    "analyzed": False,
                    "message": detail,
                },
                "results": [],
                "not_for_sale": [],
                "status": status,
            }

        visual_directions = list(dict.fromkeys([
            *(str(term).strip() for term in visual_analysis.search_terms if str(term).strip()),
            *(str(term).strip() for term in visual_analysis.visual_attributes if str(term).strip()),
        ]))[:4]
        if not visual_directions and visual_analysis.colour_palette:
            palette = ", ".join(str(value).strip() for value in visual_analysis.colour_palette if str(value).strip())
            if palette:
                visual_directions = [f"artwork with a {palette} colour palette"]
        if not visual_directions and visual_analysis.summary.strip():
            visual_directions = [visual_analysis.summary.strip()[:180]]
        if not visual_directions:
            vision_progress.update({"state": "warning", "detail": "The image analysis returned no usable search terms."})
            await stop_optional_tasks()
            detail = (
                "ArtCo received the image, but the image analysis did not produce usable art-search terms. "
                "No text-only recommendations were returned. Try a clearer image, or remove the image to run a text-only search."
            )
            return {
                "success": False,
                "error_type": "RequiredVisualSearchFailed",
                "detail": detail,
                "diagnostics": {
                    "duration_ms": round((loop.time() - started) * 1000),
                    "response_target_ms": response_target_ms,
                    "gemini_configured": bool(config.GEMINI_API_KEY),
                    "gemini_model": config.GEMINI_MODEL,
                    "text_search_completed": True,
                    "text_search_results_found": len(text_works),
                    "text_source_diagnostics": text_source_diagnostics,
                },
                "visual_inspiration": {
                    "image_count": len(request.inspiration_images),
                    "analyzed": True,
                    "summary": visual_analysis.summary,
                    "visual_attributes": visual_analysis.visual_attributes,
                    "colour_palette": visual_analysis.colour_palette,
                    "search_terms": visual_analysis.search_terms,
                    "message": detail,
                },
                "results": [],
                "not_for_sale": [],
                "status": status,
            }

        vision_progress.update({"state": "done", "detail": "Image-derived search terms are ready."})
        visual_terms = list(dict.fromkeys([
            *visual_directions,
            *(str(value).strip() for value in visual_analysis.colour_palette if str(value).strip()),
        ]))
        visual_request = request.model_copy(update={
            "art_interests": list(dict.fromkeys([*request.art_interests, *visual_terms]))[:8]
        })

        # Phase 2: run a separate provider search using image-derived terms, then
        # combine both pools for ranking. This pass is awaited, not background work.
        visual_works, visual_source_diagnostics = await _research(
            visual_directions,
            visual_request,
            status,
            phase_label="Searching for artwork that matches your images",
        )
        visual_search_performed = True
        status.append({
            "label": "Image-directed search complete",
            "state": "done" if visual_works else "warning",
            "detail": (
                f"The image search returned {len(visual_works)} catalogue records."
                if visual_works else
                "Image analysis ran and a separate image-directed search was attempted, but connected sources returned no artwork records for those visual terms."
            ),
        })
        request = visual_request

    # Wait only briefly for independent cultural enrichment; neither search phase
    # depends on it, and attached-image analysis/search has already completed.
    grace = min(0.55, max(0.0, 4.25 - (loop.time() - started)))
    pending_optional = [task for task in optional_tasks if not task.done()]
    if pending_optional and grace > 0:
        await asyncio.wait(pending_optional, timeout=grace)
    await stop_optional_tasks()

    def completed_result(task: asyncio.Task, default: Any) -> Any:
        if not task.done() or task.cancelled():
            return default
        try:
            return task.result()
        except Exception as exc:
            logger.warning("Optional concierge task failed (%s)", type(exc).__name__)
            return default

    signals = completed_result(signals_task, fallback_signals)
    if not isinstance(signals, RequestSignals):
        try:
            signals = RequestSignals.model_validate(signals.model_dump() if hasattr(signals, "model_dump") else signals)
        except Exception:
            signals = fallback_signals

    if visual_analysis:
        visual_terms = [*visual_analysis.search_terms, *visual_analysis.visual_attributes, *visual_analysis.colour_palette]
        signals.art_interests = list(dict.fromkeys([*visual_terms, *signals.art_interests]))[:8]
        request = request.model_copy(update={
            "art_interests": list(dict.fromkeys([*request.art_interests, *signals.art_interests]))[:8]
        })

    qloo_result = completed_result(qloo_task, ([], []))
    resolved, qloo_artists = qloo_result if isinstance(qloo_result, tuple) and len(qloo_result) == 2 else ([], [])
    cultural_art_directions = completed_result(qloo_direction_task, [])
    cultural_art_directions = [str(item).strip() for item in cultural_art_directions if isinstance(item, str) and item.strip()][:8]

    request = _apply_signal_overrides(request, signals)
    for entry in status:
        if entry.get("state") == "active":
            entry["state"] = "skipped"
            entry.setdefault("detail", "Skipped because the search phases had priority.")

    # Merge the original text pool and the required image-directed pool, retaining
    # distinct catalogue records and the first version of duplicate IDs.
    merged_by_id: dict[str, Artwork] = {}
    for work in [*text_works, *visual_works]:
        if _budget_ok(work, request):
            merged_by_id.setdefault(work.id, work)
    works = list(merged_by_id.values())

    source_map: dict[str, dict[str, Any]] = {}
    for phase_name, diagnostics in (("text", text_source_diagnostics), ("image", visual_source_diagnostics)):
        for stat in diagnostics:
            name = str(stat.get("provider") or "Unknown source")
            target = source_map.setdefault(name, {
                "provider": name,
                "configured": bool(stat.get("configured")),
                "queries_attempted": 0,
                "results_found": 0,
                "images_found": 0,
                "errors": [],
                "text_queries_attempted": 0,
                "image_queries_attempted": 0,
            })
            for metric in ("queries_attempted", "results_found", "images_found"):
                target[metric] += int(stat.get(metric) or 0)
            target[f"{phase_name}_queries_attempted"] += int(stat.get("queries_attempted") or 0)
            for error in stat.get("errors") or []:
                if error not in target["errors"]:
                    target["errors"].append(error)

    source_diagnostics = list(source_map.values())
    directions = list(dict.fromkeys([*text_directions, *visual_directions]))[:8]
    if not directions:
        directions = ["contemporary art"]

    # Filter to works with a direct thumbnail or approved collection preview page.
    works = [work for work in works if _has_image_candidate(work) and _budget_ok(work, request)]
    if _requires_purchasable_inventory(request):
        works = [work for work in works if _is_purchasable(work)]
    status.append({"label": "Selecting the strongest matches", "state": "done"})
    review = None
    selected = _select(works, request, qloo_artists, review, cultural_art_directions)
    review_data = _review_map(review)
    purchase_only_intent = _requires_purchasable_inventory(request)
    purchasable = [work for work in works if _is_purchasable(work)]
    # Purchase-oriented recommendations are never padded with institutional
    # records, inquiry-only listings, withdrawn works, or unavailable inventory.
    not_for_sale = [] if purchase_only_intent else [work for work in works if not _is_purchasable(work)]

    if purchase_only_intent:
        primary = [work for work in selected if _is_purchasable(work)][: min(max(1, request.number_of_works), 3)]
    elif request.intent.value == "curate":
        primary = selected[: min(max(1, request.number_of_works), 3)]
    else:
        primary = selected[:3]
    primary_ids = {work.id for work in primary}
    # Non-purchase flows may show related institutional works in a secondary
    # section. Buy never fills gaps with works that are not actually for sale.
    secondary_limit = 0
    secondary = [work for work in not_for_sale if work.id not in primary_ids][:secondary_limit]

    intent_summary = {
        "discover": "I used the things you like as cultural signals, then searched beyond the most obvious art matches.",
        "find": "I translated your request into a focused search, then compared the resulting works against the brief.",
        "taste": (
            "I used your saved artworks, taste notes, and earlier ideas to guide a search for purchasable works that extend your preferences."
            if request.existing_artworks or request.existing_ideas or request.profile_notes
            else "I used your stated interests and current brief to search for purchasable works that could expand your taste."
        ),
        "curate": "I searched purchasable works that fit your brief individually and make sense together as a group.",
        "buy": "I searched connected sellers for works explicitly listed as available to purchase. Only buyable listings are shown.",
        "learn": "I searched the requested artistic territory and selected works that help put the subject into context.",
    }[request.intent.value]
    if _uses_saved_taste(request) and request.intent.value == "discover":
        intent_summary = "I used your saved artworks, taste notes, and earlier ideas to guide an exploration of related works explicitly available to acquire."
    if visual_analysis:
        visual_message = (
            "These visual characteristics informed a separate artwork search and the relevance ranking."
            if visual_works else
            "The image was interpreted and used for a separate artwork search, but connected sources returned no records for those visual terms. The written-request results remain available, with visual characteristics included in relevance ranking."
        )
    else:
        visual_message = ""

    return {
        "success": True,
        "intent": request.intent.value,
        "diagnostics": {
            "duration_ms": round((loop.time() - started) * 1000),
            "response_target_ms": response_target_ms,
            "qloo_configured": bool(config.QLOO_API_KEY),
            "qloo_base_url": config.QLOO_BASE_URL,
            "gemini_configured": bool(config.GEMINI_API_KEY),
            "gemini_model": config.GEMINI_MODEL,
            "commercial_sources_configured": [
                name for name, configured in [
                    ("Artsy", bool(config.ARTSY_XAPP_TOKEN and config.ARTSY_PARTNER_ID)),
                    ("Artlogic", bool(config.ARTLOGIC_FEED_URL)),
                    ("Collect24", bool(config.COLLECT24_API_KEY)),
                ] if configured
            ],
            "commercial_source_count": sum(bool(value) for value in [
                config.ARTSY_XAPP_TOKEN and config.ARTSY_PARTNER_ID,
                config.ARTLOGIC_FEED_URL,
                config.COLLECT24_API_KEY,
            ]),
            "purchase_inventory_found": len(purchasable),
            "purchase_only_intent": purchase_only_intent,
            "taste_personalization_enabled": _uses_saved_taste(request),
            "taste_reference_artworks_used": len(request.existing_artworks) if _uses_saved_taste(request) else 0,
            "taste_reference_ideas_used": len(request.existing_ideas) if _uses_saved_taste(request) else 0,
            "taste_profile_notes_used": bool(request.profile_notes and _uses_saved_taste(request)),
            "institutional_works_found": sum(work.source_kind == "institution" for work in works),
            "image_bearing_works_found": len(works),
            "source_diagnostics": source_diagnostics,
            "text_search": {
                "performed": True,
                "directions": text_directions,
                "results_found": len(text_works),
                "source_diagnostics": text_source_diagnostics,
            },
            "image_search": {
                "required": has_images,
                "performed": visual_search_performed,
                "directions": visual_directions,
                "results_found": len(visual_works),
                "source_diagnostics": visual_source_diagnostics,
            },
            "entity_matches_found": len(resolved),
            "related_artists_found": len(qloo_artists),
            "cultural_art_directions_used": len(cultural_art_directions),
        },
        "summary": intent_summary,
        "learning_guide": _build_learning_guide(request.goal, primary) if request.intent.value == "learn" else None,
        "visual_inspiration": {
            "image_count": len(request.inspiration_images),
            "analyzed": visual_analysis is not None,
            "summary": visual_analysis.summary if visual_analysis else "",
            "visual_attributes": visual_analysis.visual_attributes if visual_analysis else [],
            "colour_palette": visual_analysis.colour_palette if visual_analysis else [],
            "search_terms": visual_analysis.search_terms if visual_analysis else [],
            "message": visual_message,
        },
        "brief": {
            "intent": request.intent.value,
            "request": request.goal or "",
            "avoid_references": request.avoid_references,
            "art_interests": request.art_interests,
            "mediums": request.mediums,
            "room": request.room,
            "size_preference": request.size_preference,
            "preferred_market": request.preferred_market,
            "budget_min": request.budget_min,
            "budget_max": request.budget_max,
            "number_of_works": min(max(1, request.number_of_works), 8),
            "discovery_level": request.discovery_level,
            "personalize_with_saved_taste": _uses_saved_taste(request),
        },
        "taste": {
            "inputs": list(dict.fromkeys(request.loves + signals.cultural_references + request.additional_interests + (request.existing_ideas if _uses_saved_taste(request) else [])))[:12],
            "reference_artworks": [item.model_dump() for item in request.existing_artworks[:6]] if _uses_saved_taste(request) else [],
            "avoid_references": request.avoid_references[:12],
            "resolved": resolved[:10],
            "related_artists": qloo_artists[:8],
        },
        "search_directions": directions,
        "cultural_art_directions": cultural_art_directions,
        "search_directions_used": list(dict.fromkeys(item.get("label", "") for item in status if item.get("label"))),
        "critique": review_data.get("critique", ""),
        "results": [_serialize(work, request, qloo_artists, review_data, cultural_art_directions) for work in primary],
        "not_for_sale": [] if purchase_only_intent else [_serialize(work, request, qloo_artists, review_data, cultural_art_directions) for work in secondary],
        "purchase_available": len(purchasable),
        "commercial_found": sum(work.source_kind == "commercial" for work in works),
        "institutional_found": sum(work.source_kind == "institution" for work in works),
        "total_found": len(works),
        "status": status,
    }



def _serialize(
    work: Artwork,
    request: ConciergeRequest,
    qloo_artists: list[dict[str, Any]],
    review_data: dict[str, Any],
    cultural_art_directions: list[str] | None = None,
) -> dict[str, Any]:
    assessment = review_data.get("assessments", {}).get(work.id) or {}
    reasons: list[str] = []
    fit_reason = assessment.get("fit_reason")
    if fit_reason:
        reasons.append(str(fit_reason))

    matching_artist = next(
        (item for item in qloo_artists if item.get("name") and str(item.get("name")).strip().casefold() == work.artist.strip().casefold()),
        None,
    )
    bridge_terms = set().union(*(_tokenize(value) for value in (cultural_art_directions or []))) if cultural_art_directions else set()
    metadata = " ".join(part for part in [work.title, work.artist, work.medium, work.description] if part).casefold()
    if bridge_terms and any(token in metadata for token in bridge_terms) and len(reasons) < 2:
        reasons.append("Shares artistic qualities connected to your broader interests")
    if matching_artist and len(reasons) < 2:
        reasons.append("Connects with your broader cultural taste")
    if request.discovery_level >= 70 and len(reasons) < 2:
        reasons.append("Leaves room for discovery")
    elif request.discovery_level <= 30 and len(reasons) < 2:
        reasons.append("Stays close to what you already like")
    if not reasons:
        reasons.append("Fits the direction the concierge researched for you")

    return {
        "id": work.id,
        "title": work.title,
        "artist": work.artist,
        "image_url": _normalise_image_url(work.image_url),
        "display_image_url": display_image_url(_normalise_image_url(work.image_url)),
        "detail_url": work.detail_url,
        "source": work.source,
        "source_kind": work.source_kind,
        "availability": work.availability,
        "purchase_mode": work.purchase_mode,
        "seller_name": work.seller_name,
        "checkout_available": bool(checkout_catalog_entry(work.id)),
        "price": work.price,
        "currency": work.currency,
        "medium": work.medium,
        "dimensions": work.dimensions,
        "year": work.year,
        "description": work.description,
        "matched_direction": work.matched_direction,
        "why": reasons[:2],
        "concern": assessment.get("concern", ""),
        "fit_score": assessment.get("score"),
        "price_label": (
            f"{work.currency} {work.price:,.0f}" if work.price is not None and work.currency else
            f"{work.price:,.0f}" if work.price is not None else
            "Price on request" if work.source_kind == "commercial" and work.availability == "available" else
            None
        ),
    }

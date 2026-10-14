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
            request.intent.value,
            request.goal or "",
            signals.cultural_references,
            list(dict.fromkeys(request.art_interests + signals.art_interests)),
            list(dict.fromkeys(request.mediums + signals.mediums)),
            qloo_artists,
            request.discovery_level,
            feedback=(request.feedback or "") + (f" Avoid references: {request.avoid_references}" if request.avoid_references else ""),
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
        "curate": ["contemporary painting", "art photography", "modern sculpture"],
        "taste": ["contemporary painting", "abstract art", "figurative art"],
        "learn": ["art history", "modern art", "contemporary art"],
    }
    fallbacks = fallback_by_intent.get(
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


def _tokenize(text: str | None) -> set[str]:
    return {token.lower() for token in (text or "").replace("-", " ").split() if len(token) > 3}


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
    haystack = f"{metadata_haystack} {direction}".casefold()
    score = 0.0
    # The optional culture-to-art bridge translates Qloo associations into art
    # catalogue terms. These terms only influence the score when they match
    # actual artwork metadata, not merely the provider query string.
    bridge_terms = set().union(*(_tokenize(value) for value in (cultural_art_directions or []))) if cultural_art_directions else set()
    bridge_matches = sum(1 for token in bridge_terms if token in metadata_haystack)
    score += min(12, bridge_matches * 2)
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
    score += min(18, 3 * sum(1 for token in request_terms if token in haystack))
    if work.image_url:
        score += 4
    if request.purchase_required:
        score += 35 if work.source_kind == "commercial" and work.availability == "available" else -40
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
    """True when a work has a direct thumbnail or an approved collection page for preview fallback."""
    return _usable_image_url(work.image_url) or is_allowed_detail_url(work.detail_url or "")


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
) -> tuple[list[Artwork], list[dict[str, Any]]]:
    """Search a bounded set of providers in parallel.

    Previous builds multiplied up to five directions by every provider, then did
    a second full fan-out and a second model review. This version makes one query
    per provider in each pass and only expands the source set when image-bearing
    results are insufficient.
    """
    progress = {"label": "Researching artwork sources", "state": "active"}
    status.append(progress)
    commercial = commercial_providers()
    institutional = institutional_providers()
    if not include_extended_sources:
        institutional = [provider for provider in institutional if provider.name != "Rijksmuseum"]

    if request.intent.value == "learn":
        provider_groups = [institutional, commercial]
    elif request.purchase_required:
        provider_groups = [commercial, institutional]
    else:
        provider_groups = [institutional, commercial]

    fallback_by_intent = {
        "buy": ["contemporary art for sale", "original contemporary painting"],
        "curate": ["contemporary painting", "art photography"],
        "taste": ["contemporary painting", "abstract art"],
        "learn": ["modern art", "art history"],
    }
    fallback = fallback_by_intent.get(request.intent.value, ["contemporary art", "modern painting", "contemporary photography"])
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
    avoided_artist_refs = [
        " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))
        for value in request.avoid_references
        if str(value).strip()
    ]

    def candidate_is_avoided(work: Artwork) -> bool:
        if work.id in excluded_ids:
            return True
        artist = " ".join(re.findall(r"[a-z0-9]+", str(work.artist or "").lower()))
        if not artist or re.fullmatch(r"(unknown|anonymous|various) artist", artist):
            return False
        return any(
            reference and (artist == reference or f" {reference} " in f" {artist} ")
            for reference in avoided_artist_refs
        )

    ranked = sorted(
        (work for work in works if _has_image_candidate(work) and not candidate_is_avoided(work)),
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
        if request.purchase_required and not (work.source_kind == "commercial" and work.availability == "available"):
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


async def run_concierge(request: ConciergeRequest) -> dict[str, Any]:
    """Run a time-bounded concierge search and return an image-ready shortlist.

    Catalogue search, request interpretation, optional image analysis, and Qloo
    enrichment run concurrently. The response uses deterministic ranking rather
    than waiting for a second model-review pass, keeping the response budget under
    five seconds when at least one public catalogue responds promptly.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    response_budget_seconds = 4.25
    status: list[dict[str, str]] = [{"label": "Request received", "state": "done"}]

    fallback_signals = RequestSignals(
        cultural_references=list(dict.fromkeys(
            request.loves + request.additional_interests
        ))[:10],
        art_interests=request.art_interests[:8],
        mediums=request.mediums[:6],
        room=request.room,
        size_preference=request.size_preference,
        market=request.preferred_market,
        budget_min=request.budget_min,
        budget_max=request.budget_max,
    )
    directions = _fast_search_directions(request)

    # Kick off independent enrichment immediately. A short, bounded opportunity
    # lets very fast vision/model responses inform the first queries without ever
    # putting a full model round-trip in front of catalogue search.
    signals_task = asyncio.create_task(extract_request_signals(
        request.goal or "",
        request.loves,
        request.additional_interests + request.art_interests,
        request.avoid_references,
    ))
    visual_task = (
        asyncio.create_task(analyze_inspiration_images(request.goal or "", request.inspiration_images))
        if request.inspiration_images else None
    )
    async def qloo_after_signal_extraction() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Qloo should resolve only references extracted from the current brief and
        # explicit positive taste. Give extraction a short opportunity to finish;
        # if the model is slow, use explicit profile fields rather than querying
        # the entire sentence and accidentally resolving an unrelated entity.
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
    early_enrichment = [signals_task] + ([visual_task] if visual_task else [])
    if early_enrichment:
        # Vision needs a realistic share of the deadline: 350 ms caused uploaded
        # images to be omitted from the initial catalogue queries almost every time.
        await asyncio.wait(early_enrichment, timeout=1.1 if visual_task else 0.55)
    early_signals = signals_task.result() if signals_task.done() and not signals_task.cancelled() and signals_task.exception() is None else fallback_signals
    if not isinstance(early_signals, RequestSignals):
        try:
            early_signals = RequestSignals.model_validate(early_signals.model_dump() if hasattr(early_signals, "model_dump") else early_signals)
        except Exception:
            early_signals = fallback_signals
    if visual_task and visual_task.done() and not visual_task.cancelled() and visual_task.exception() is None:
        early_visual = visual_task.result()
    else:
        early_visual = None
    fallback_directions = _fast_search_directions(request)
    search_goal = _searchable_goal(request)
    directions = list(dict.fromkeys([
        search_goal,
        *((early_visual.search_terms[:3] if early_visual else [])),
        *((early_visual.visual_attributes[:1] if early_visual else [])),
        *early_signals.art_interests[:2],
        *request.art_interests[:2],
        *request.mediums[:1],
        *[item for item in fallback_directions if item != search_goal],
    ]))[:4]
    directions = [direction for direction in directions if direction]
    research_task = asyncio.create_task(_research(directions, request, status))
    tasks = [research_task, signals_task, qloo_task, qloo_direction_task] + ([visual_task] if visual_task else [])

    # Only the catalogue search is on the critical path. Enrichment shares its
    # runtime, but never delays the response after the art sources have answered.
    wait_budget = min(3.45, max(0.05, response_budget_seconds - (loop.time() - started) - 0.45))
    try:
        await asyncio.wait_for(asyncio.shield(research_task), timeout=wait_budget)
    except Exception as exc:
        logger.warning("Primary artwork search did not finish within the response budget (%s)", type(exc).__name__)
        if not research_task.done():
            research_task.cancel()

    # Give concurrent enrichment a small grace period after the catalogue result
    # is ready, but preserve the overall response deadline.
    optional_pending = [task for task in tasks if task is not research_task and not task.done()]
    grace = min(0.55, max(0.0, response_budget_seconds - (loop.time() - started) - 0.25))
    if optional_pending and grace > 0:
        await asyncio.wait(optional_pending, timeout=grace)
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    def completed_result(task: asyncio.Task, default: Any) -> Any:
        if not task.done() or task.cancelled():
            return default
        try:
            return task.result()
        except Exception as exc:
            logger.warning("Optional concierge task failed (%s)", type(exc).__name__)
            return default

    research_result = completed_result(research_task, ([], []))
    works, source_diagnostics = research_result
    signals = completed_result(signals_task, fallback_signals)
    if not isinstance(signals, RequestSignals):
        try:
            signals = RequestSignals.model_validate(signals.model_dump() if hasattr(signals, "model_dump") else signals)
        except Exception:
            signals = fallback_signals
    if visual_task:
        visual_analysis = completed_result(visual_task, None) or early_visual
    else:
        visual_analysis = None

    qloo_result = completed_result(qloo_task, ([], []))
    resolved, qloo_artists = qloo_result if isinstance(qloo_result, tuple) and len(qloo_result) == 2 else ([], [])
    cultural_art_directions = completed_result(qloo_direction_task, [])
    cultural_art_directions = [str(item).strip() for item in cultural_art_directions if isinstance(item, str) and item.strip()][:8]

    if visual_analysis:
        visual_terms = [*visual_analysis.search_terms, *visual_analysis.visual_attributes]
        signals.art_interests = list(dict.fromkeys([*visual_terms, *signals.art_interests]))[:8]
        status.append({"label": "Interpreting your inspiration images", "state": "done"})
    elif request.inspiration_images:
        status.append({
            "label": "Visual analysis unavailable", "state": "warning",
            "detail": "Images were received, but the visual analysis did not finish within the response time budget. The search used the written brief and saved taste.",
        })

    request = _apply_signal_overrides(request, signals)
    for entry in status:
        if entry.get("state") == "active":
            entry["state"] = "skipped"
            entry.setdefault("detail", "Skipped to preserve the response time budget.")
    if not source_diagnostics:
        status.append({"label": "Artwork sources did not respond in time", "state": "warning"})
    if not directions:
        directions = ["contemporary art"]

    # Only works with a direct thumbnail or a verified public collection page
    # enter visible recommendations. The page is a fallback for missing API thumbnails.
    works = [work for work in works if _has_image_candidate(work) and _budget_ok(work, request)]
    status.append({"label": "Selecting the strongest matches", "state": "done"})
    # Deterministic fit scoring is immediate; model-based candidate review was
    # removed from the blocking path because it added a second serial LLM round.
    review = None
    selected = _select(works, request, qloo_artists, review, cultural_art_directions)
    review_data = _review_map(review)
    purchasable = [
        work for work in works
        if work.source_kind == "commercial" and work.availability == "available"
    ]
    not_for_sale = [work for work in works if not (work.source_kind == "commercial" and work.availability == "available")]

    if request.purchase_required:
        primary = [work for work in selected if work.source_kind == "commercial" and work.availability == "available"][: min(max(1, request.number_of_works), 3)]
    elif request.intent.value == "curate":
        primary = selected[: min(max(1, request.number_of_works), 3)]
    else:
        primary = selected[:3]
    primary_ids = {work.id for work in primary}
    # Three is the total visible shortlist across both sections. Only Buy uses
    # the secondary section to fill gaps when fewer than three works can be bought.
    secondary_limit = max(0, 3 - len(primary)) if request.intent.value == "buy" else 0
    secondary = [work for work in not_for_sale if work.id not in primary_ids][:secondary_limit]

    intent_summary = {
        "discover": "I used the things you like as cultural signals, then searched beyond the most obvious art matches.",
        "find": "I translated your request into a focused search, then compared the resulting works against the brief.",
        "taste": "I started from your references and explored related artists and works to help you understand what you respond to.",
        "curate": "I looked for works that fit your brief individually and make sense together as a group.",
        "buy": "I prioritised commercial works marked as available to acquire. Additional works that are not for sale are shown separately when useful.",
        "learn": "I searched the requested artistic territory and selected works that help put the subject into context.",
    }[request.intent.value]

    return {
        "success": True,
        "intent": request.intent.value,
        "diagnostics": {
            "duration_ms": round((loop.time() - started) * 1000),
            "response_target_ms": 4500,
            "qloo_configured": bool(config.QLOO_API_KEY),
            "qloo_base_url": config.QLOO_BASE_URL,
            "gemini_configured": bool(config.GEMINI_API_KEY),
            "commercial_sources_configured": [
                name for name, configured in [
                    ("Artsy", bool(config.ARTSY_XAPP_TOKEN and config.ARTSY_PARTNER_ID)),
                    ("Artlogic", bool(config.ARTLOGIC_FEED_URL)),
                    ("Collect24", bool(config.COLLECT24_API_KEY)),
                ] if configured
            ],
            "purchase_inventory_found": len(purchasable),
            "institutional_works_found": sum(work.source_kind == "institution" for work in works),
            "image_bearing_works_found": len(works),
            "source_diagnostics": source_diagnostics,
            "entity_matches_found": len(resolved),
            "related_artists_found": len(qloo_artists),
            "cultural_art_directions_used": len(cultural_art_directions),
        },
        "summary": intent_summary,
        "visual_inspiration": {
            "image_count": len(request.inspiration_images),
            "analyzed": visual_analysis is not None,
            "summary": visual_analysis.summary if visual_analysis else "",
            "visual_attributes": visual_analysis.visual_attributes if visual_analysis else [],
            "colour_palette": visual_analysis.colour_palette if visual_analysis else [],
            "search_terms": visual_analysis.search_terms if visual_analysis else [],
            "message": (
                "These visible characteristics informed the search."
                if visual_analysis
                else ("The images were not interpreted this time; the search used your written request and saved taste." if request.inspiration_images else "")
            ),
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
        },
        "taste": {
            "inputs": list(dict.fromkeys(request.loves + signals.cultural_references + request.additional_interests))[:12],
            "avoid_references": request.avoid_references[:12],
            "resolved": resolved[:10],
            "related_artists": qloo_artists[:8],
        },
        "search_directions": directions,
        "cultural_art_directions": cultural_art_directions,
        "search_directions_used": list(dict.fromkeys(item.get("label", "") for item in status if item.get("label"))),
        "critique": review_data.get("critique", ""),
        "results": [_serialize(work, request, qloo_artists, review_data, cultural_art_directions) for work in primary],
        "not_for_sale": [_serialize(work, request, qloo_artists, review_data, cultural_art_directions) for work in secondary],
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

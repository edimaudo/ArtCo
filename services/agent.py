from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from models.schemas import ConciergeRequest
from .art_sources import Artwork, commercial_providers, institutional_providers
from .llm import CandidateReview, extract_request_signals, plan_art_searches, review_candidates
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
    queries = list(
        dict.fromkeys(
            [
                *request.loves,
                *request.avoid_references,
                *signals.cultural_references,
                *request.art_interests,
                *request.additional_interests,
            ]
        )
    )[:12]

    # With no explicit references, allow Qloo to resolve the user's request itself.
    if not queries and request.goal:
        queries = [request.goal[:160]]

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
) -> float:
    haystack = " ".join(
        part for part in [work.title, work.artist, work.medium, work.description, direction] if part
    ).lower()
    score = 0.0
    qloo_names = {str(item.get("name") or "").lower() for item in qloo_artists if item.get("name")}
    avoid_names = {str(value).strip().lower() for value in request.avoid_references if str(value).strip()}
    if work.artist.strip().lower() in avoid_names or any(value in work.artist.strip().lower() for value in avoid_names):
        score -= 35
    if work.artist.lower() in qloo_names or any(name and name in work.artist.lower() for name in qloo_names):
        score += 18
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
        works = await provider.search(direction, limit=limit)
        return provider.name, direction, [replace(work, matched_direction=direction) for work in works], None
    except Exception as exc:
        logger.exception("Artwork provider %s failed while searching direction %r", provider.name, direction)
        return provider.name, direction, [], _safe_provider_error(exc)


async def _research(
    directions: list[str],
    request: ConciergeRequest,
    status: list[dict[str, str]],
) -> tuple[list[Artwork], list[dict[str, Any]]]:
    progress = {"label": "Researching artwork sources", "state": "active"}
    status.append(progress)
    commercial = commercial_providers()
    institutional = institutional_providers()

    if request.intent.value == "learn":
        provider_groups = [institutional, commercial]
    elif request.purchase_required:
        provider_groups = [commercial, institutional]
    else:
        provider_groups = [institutional, commercial]

    # Use a bounded mix of specific and broad terms. Cultural items such as a
    # favourite musician are useful signals for Qloo, but often return no rows
    # when sent verbatim to a museum artwork catalogue. Always reserve searches
    # for broad visual-art categories so source coverage does not collapse.
    fallback_by_intent = {
        "buy": ["contemporary art for sale", "original contemporary painting"],
        "curate": ["contemporary painting", "art photography"],
        "taste": ["contemporary painting", "abstract art"],
        "learn": ["modern art", "art history"],
    }
    fallback = fallback_by_intent.get(request.intent.value, ["contemporary art", "modern painting"])
    specific = list(dict.fromkeys(
        [*request.art_interests[:2], *request.mediums[:1], *directions[:3]]
    ))[:3]
    search_directions = list(dict.fromkeys([*specific, *fallback, *directions[3:]]))[:5]
    if not search_directions:
        search_directions = ["contemporary art", "modern painting", "contemporary photography"]

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
            "errors": [],
        }
        if configured:
            enabled_providers.append(provider)

    tasks = [
        _search_provider(provider, direction, limit=6)
        for provider in enabled_providers
        for direction in search_directions
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
                works.extend(batch)

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
    if not deduped:
        logger.warning("No artwork candidates found for queries %s; source diagnostics=%s", search_directions, source_stats)
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
) -> list[Artwork]:
    review_by_id = {item.artwork_id: item for item in (review.assessments if review else [])}

    ranked = sorted(
        works,
        key=lambda work: (
            review_by_id.get(work.id).score if work.id in review_by_id else _deterministic_score(work, request, qloo_artists, work.matched_direction or ""),
            _deterministic_score(work, request, qloo_artists, work.matched_direction or ""),
        ),
        reverse=True,
    )

    seen_artists: set[str] = set()
    selected: list[Artwork] = []
    target = max(1, request.number_of_works)
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
        if len(selected) >= (target if request.intent.value in {"curate", "buy"} else 8):
            break
    return selected


def _should_review_again(request: ConciergeRequest, review: CandidateReview | None, works: list[Artwork]) -> bool:
    if review is None:
        if request.purchase_required:
            return sum(w.source_kind == "commercial" and w.availability == "available" for w in works) < 3
        return len(works) < max(6, request.number_of_works + 3)
    return bool(review.follow_up_queries)


async def run_concierge(request: ConciergeRequest) -> dict[str, Any]:
    """Run the native Python concierge loop with model-assisted planning and critique."""
    status: list[dict[str, str]] = [{"label": "Request received", "state": "done"}]

    signals = await extract_request_signals(
        request.goal or "",
        request.loves,
        request.additional_interests + request.art_interests,
        request.avoid_references,
    )
    request = _apply_signal_overrides(request, signals)

    resolved = await _resolve_entities(request, status, signals)
    qloo_artists = await _qloo_taste(resolved, request, status)
    directions = await _build_search_plan(request, signals, qloo_artists, status)

    works, source_diagnostics = await _research(directions, request, status)

    status.append({"label": "Reviewing the first set of results", "state": "active"})
    first_review = await review_candidates(
        request.intent.value,
        request.goal or "",
        [_candidate_payload(work) for work in works],
        request.discovery_level,
        request.purchase_required,
        request.number_of_works,
    )
    status[-1]["state"] = "done"

    review = first_review
    follow_up_queries = first_review.follow_up_queries if first_review else []
    if _should_review_again(request, first_review, works) and not follow_up_queries:
        follow_up_queries = list(dict.fromkeys(directions + ["adjacent contemporary art", "emerging contemporary artists"]))[-3:]

    if follow_up_queries:
        status.append({"label": "Looking again where the first search was weak", "state": "active"})
        second_pass, second_source_diagnostics = await _research(follow_up_queries, request, status)
        works.extend(second_pass)
        stats_by_provider = {item["provider"]: item for item in source_diagnostics}
        for new_stat in second_source_diagnostics:
            current = stats_by_provider.get(new_stat["provider"])
            if current is None:
                stats_by_provider[new_stat["provider"]] = new_stat
                continue
            current["configured"] = current["configured"] or new_stat["configured"]
            current["queries_attempted"] += new_stat["queries_attempted"]
            current["results_found"] += new_stat["results_found"]
            current["errors"] = list(dict.fromkeys(current["errors"] + new_stat["errors"]))
        source_diagnostics = list(stats_by_provider.values())
        status[-1]["state"] = "done"

        deduped: dict[str, Artwork] = {work.id: work for work in works}
        works = list(deduped.values())
        status.append({"label": "Making the final selection", "state": "active"})
        final_review = await review_candidates(
            request.intent.value,
            request.goal or "",
            [_candidate_payload(work) for work in works],
            request.discovery_level,
            request.purchase_required,
            request.number_of_works,
        )
        if final_review is not None:
            review = final_review
        status[-1]["state"] = "done"
    else:
        status.append({"label": "Making the final selection", "state": "done"})

    selected = _select(works, request, qloo_artists, review)
    review_data = _review_map(review)

    # For Buy, keep only genuine commercial inventory in the primary results.
    purchasable = [
        work for work in works
        if work.source_kind == "commercial" and work.availability == "available"
    ]
    not_for_sale = [work for work in works if not (work.source_kind == "commercial" and work.availability == "available")]

    if request.purchase_required:
        primary = [work for work in selected if work.source_kind == "commercial" and work.availability == "available"][: request.number_of_works]
    elif request.intent.value == "curate":
        primary = selected[: request.number_of_works]
    else:
        primary = selected[:8]
    primary_ids = {work.id for work in primary}
    secondary = [work for work in not_for_sale if work.id not in primary_ids][:4]

    intent_summary = {
        "discover": "I used the things you like as cultural signals, then searched beyond the most obvious art matches.",
        "find": "I translated your request into a focused search, then compared the resulting works against the brief.",
        "taste": "I started from your references and explored related artists and works to help you understand what you respond to.",
        "curate": "I looked for works that fit your brief individually and make sense together as a group.",
        "buy": "I prioritised commercial works marked as available to acquire. Works from institutional sources are separated below when they are useful references.",
        "learn": "I searched the requested artistic territory and selected works that help put the subject into context.",
    }[request.intent.value]

    return {
        "success": True,
        "intent": request.intent.value,
        "diagnostics": {
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
            "source_diagnostics": source_diagnostics,
            "entity_matches_found": len(resolved),
            "related_artists_found": len(qloo_artists),
        },
        "summary": intent_summary,
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
            "number_of_works": request.number_of_works,
            "discovery_level": request.discovery_level,
        },
        "taste": {
            "inputs": list(dict.fromkeys(request.loves + signals.cultural_references + request.additional_interests))[:12],
            "avoid_references": request.avoid_references[:12],
            "resolved": resolved[:10],
            "related_artists": qloo_artists[:8],
        },
        "search_directions": directions,
        "search_directions_used": list(dict.fromkeys(
            item.get("label", "") for item in status if item.get("label")
        )),
        "critique": review_data.get("critique", ""),
        "results": [_serialize(work, request, qloo_artists, review_data) for work in primary],
        "not_for_sale": [_serialize(work, request, qloo_artists, review_data) for work in secondary],
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
) -> dict[str, Any]:
    assessment = review_data.get("assessments", {}).get(work.id) or {}
    reasons: list[str] = []
    fit_reason = assessment.get("fit_reason")
    if fit_reason:
        reasons.append(str(fit_reason))

    matching_artist = next(
        (item for item in qloo_artists if item.get("name") and str(item.get("name")).lower() in work.artist.lower()),
        None,
    )
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
        "image_url": work.image_url,
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

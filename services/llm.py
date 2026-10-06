from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from .config import GEMINI_API_KEY, GEMINI_MODEL

T = TypeVar("T", bound=BaseModel)


class RequestSignals(BaseModel):
    """Signals extracted from a natural-language concierge request."""

    cultural_references: list[str] = Field(default_factory=list, max_length=10)
    art_interests: list[str] = Field(default_factory=list, max_length=8)
    mediums: list[str] = Field(default_factory=list, max_length=6)
    room: str | None = None
    size_preference: str | None = None
    market: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    number_of_works: int | None = Field(default=None, ge=1, le=8)


class SearchPlan(BaseModel):
    """Search directions the concierge can execute through artwork providers."""

    queries: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = ""


class CandidateAssessment(BaseModel):
    """Evaluation of one artwork candidate against the user's brief."""

    artwork_id: str
    score: float = Field(ge=0, le=100)
    fit_reason: str = ""
    concern: str = ""


class CandidateReview(BaseModel):
    """Agent review of a candidate pool, including whether another search is needed."""

    assessments: list[CandidateAssessment] = Field(default_factory=list, max_length=40)
    follow_up_queries: list[str] = Field(default_factory=list, max_length=5)
    critique: str = ""
    coverage: str = ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _generate_structured(model: type[T], prompt: str) -> T | None:
    if not GEMINI_API_KEY:
        return None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=model,
                temperature=0.2,
            ),
        )
        text = getattr(response, "text", "") or ""
        payload = _extract_json_object(text)
        if payload is None:
            return None
        return model.model_validate(payload)
    except Exception:
        return None


async def extract_request_signals(
    request_text: str,
    existing_loves: list[str],
    existing_interests: list[str],
    avoid_references: list[str] | None = None,
) -> RequestSignals:
    """Extract actionable signals from the user's natural-language brief."""
    prompt = f"""
You are the intake analyst for a personal art concierge.
Extract only useful signals from the user's request. Do not invent preferences.
Named cultural references may include artists, musicians, filmmakers, films,
brands, fashion houses, places, books, restaurants, architects, designers,
or other recognizable cultural entities.

Existing saved references: {existing_loves}
Existing art interests: {existing_interests}
Avoid references: {avoid_references or []}
User request:
{request_text}

Return structured JSON only. Keep the lists concise.
""".strip()

    result = await _generate_structured(RequestSignals, prompt)
    if result is not None:
        return result

    # Safe fallback when Gemini is unavailable. Explicit saved references remain authoritative.
    references = list(dict.fromkeys(existing_loves + existing_interests))[:10]
    if request_text:
        quoted = re.findall(r'["“](.*?)["”]', request_text)
        references.extend(item.strip() for item in quoted if item.strip())
    return RequestSignals(cultural_references=list(dict.fromkeys(references))[:10])


async def plan_art_searches(
    intent: str,
    user_request: str,
    cultural_references: list[str],
    art_interests: list[str],
    mediums: list[str],
    qloo_artists: list[dict[str, Any]],
    discovery_level: int,
    feedback: str | None = None,
    room: str | None = None,
    budget_max: float | None = None,
    preferred_market: str | None = None,
) -> SearchPlan:
    """Create executable artwork-search directions using the user's brief and Qloo signals."""
    if GEMINI_API_KEY:
        artist_names = [a.get("name") for a in qloo_artists if a.get("name")][:12]
        prompt = f"""
You are the search planner inside a personal art concierge.
Create 4 to 6 concrete search queries for artwork catalogues.
Queries must be useful to an art database: use artists, movements, styles,
subjects, media, periods, or combinations thereof. Do not include conversational
phrasing, budget amounts, or unsupported claims.

The user's request is the primary brief. Qloo is cultural context, not proof that
someone will like a specific artwork.

Intent: {intent}
User request: {user_request}
Cultural references: {cultural_references}
Art interests: {art_interests}
Preferred media: {mediums}
Artists surfaced through Qloo: {artist_names}
Discovery level (0 familiar, 100 adventurous): {discovery_level}
Room/context: {room}
Budget ceiling: {budget_max}
Preferred market: {preferred_market}
Previous feedback: {feedback}

Return JSON only with queries and a one-sentence rationale.
""".strip()
        result = await _generate_structured(SearchPlan, prompt)
        if result is not None and result.queries:
            return result

    directions: list[str] = []
    directions.extend(art_interests[:4])
    directions.extend(mediums[:3])
    directions.extend(str(item) for item in cultural_references[:4] if item)
    directions.extend(str(item.get("name")) for item in qloo_artists[:6] if item.get("name"))
    if discovery_level >= 70:
        directions.extend(["adjacent contemporary art", "emerging contemporary artists", "cross-disciplinary art"])
    elif discovery_level <= 30:
        directions.extend(["modern art", "minimalist art", "figurative contemporary art"])
    else:
        directions.extend(["contemporary art", "modern painting", "contemporary photography"])
    if feedback:
        directions.append(feedback)
    if not directions:
        directions = ["contemporary art", "modern art", "painting", "photography"]
    cleaned = list(dict.fromkeys(item.strip() for item in directions if item.strip()))[:6]
    return SearchPlan(queries=cleaned, rationale="The search combines your request with related cultural directions and your discovery preference.")


async def review_candidates(
    intent: str,
    user_request: str,
    candidates: list[dict[str, Any]],
    discovery_level: int,
    purchase_required: bool,
    number_of_works: int,
) -> CandidateReview | None:
    """Evaluate candidate works and decide whether the search needs another pass."""
    if not GEMINI_API_KEY or not candidates:
        return None

    compact = []
    for item in candidates[:36]:
        compact.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "artist": item.get("artist"),
                "medium": item.get("medium"),
                "dimensions": item.get("dimensions"),
                "year": item.get("year"),
                "price": item.get("price"),
                "currency": item.get("currency"),
                "availability": item.get("availability"),
                "source_kind": item.get("source_kind"),
                "description": item.get("description"),
                "matched_direction": item.get("matched_direction"),
            }
        )

    prompt = f"""
You are the evaluation layer of a personal art concierge.
Review the candidate artworks against the user's actual request.

Hard rules:
- Never treat an institutional work as available for purchase.
- Purchase-required requests must favor commercial works marked available.
- Do not invent facts about artists or artworks.
- Discovery level controls how far outside the user's stated taste you may go.
- Prefer variety when curating multiple works.
- A candidate can score highly without matching an exact artist if the cultural relationship is strong.
- Identify genuine gaps that warrant another search instead of forcing weak matches.

Intent: {intent}
User request: {user_request}
Discovery level: {discovery_level}
Purchase required: {purchase_required}
Requested works: {number_of_works}

Candidates:
{json.dumps(compact, ensure_ascii=False)}

Return JSON only. Assess strong candidates, give a concise fit reason and concern,
and supply 0 to 5 concrete follow-up search queries only when the current pool has a meaningful gap.
""".strip()
    return await _generate_structured(CandidateReview, prompt)

from __future__ import annotations

import json
import re
from typing import Any

from .config import GEMINI_API_KEY, GEMINI_MODEL


async def plan_art_searches(
    intent: str,
    loves: list[str],
    art_interests: list[str],
    qloo_artists: list[dict[str, Any]],
    discovery_level: int,
    feedback: str | None = None,
    room: str | None = None,
    budget_max: float | None = None,
) -> list[str]:
    """Use Gemini as an advisory search-planning tool; orchestration stays in Python."""
    if not GEMINI_API_KEY:
        return []

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        artist_names = [a.get("name") for a in qloo_artists if a.get("name")][:10]
        prompt = f"""
You are the search-planning tool inside a personal art concierge.
Create 4 to 6 concise search queries for an artwork catalogue.
Do not recommend purchases and do not invent facts.
Use only the user's inputs and Qloo-returned artist names.
Discovery level {discovery_level}: low stays close to stated taste; high may explore adjacent directions.
Return JSON only as {{"queries": ["..."]}}.

Intent: {intent}
Cultural inputs: {loves}
Art interests: {art_interests}
Qloo artists: {artist_names}
Room/context: {room}
Budget ceiling: {budget_max}
Feedback from the user: {feedback}
""".strip()
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = getattr(response, "text", "") or ""
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return []
        payload = json.loads(match.group(0))
        queries = payload.get("queries", [])
        if not isinstance(queries, list):
            return []
        return [str(query).strip() for query in queries if str(query).strip()][:6]
    except Exception:
        return []

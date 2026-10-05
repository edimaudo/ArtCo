from __future__ import annotations

from typing import Any

from models.schemas import ConciergeRequest
from .agent import run_concierge


async def run(request: ConciergeRequest) -> dict[str, Any]:
    """Thin service boundary for the API layer."""
    return await run_concierge(request)

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from models.schemas import ConciergeRequest, Intent
import services.agent as agent
from services.art_sources import Artwork


class FakeProvider:
    def __init__(self, name: str, works_by_query: dict[str, list[Artwork]]):
        self.name = name
        self.works_by_query = works_by_query

    async def search(self, query: str, limit: int = 18):
        return self.works_by_query.get(query, [])[:limit]


def work(id_: str, artist: str, source_kind: str = "institution", availability: str = "not_for_sale", price=None, medium="painting"):
    return Artwork(
        id=id_, title=f"{artist} work", artist=artist, image_url="https://example.com/image.jpg",
        detail_url="https://example.com/work", source="Fake", source_kind=source_kind,
        availability=availability, price=price, currency="CAD" if price is not None else None,
        medium=medium, dimensions="60 x 80 cm", year="2024", description="A test artwork.",
    )


async def main():
    calls = {"extract": [], "qloo_search": [], "plan": [], "review": []}

    async def fake_extract(goal, existing_loves, existing_interests, avoid_references=None):
        calls["extract"].append(goal)
        return SimpleNamespace(
            cultural_references=["Issey Miyake", "Radiohead"], art_interests=["minimalism"],
            mediums=["painting"], room="living room", size_preference=None, market="Canada",
            budget_min=None, budget_max=5000, number_of_works=1,
        )

    async def fake_search(query):
        calls["qloo_search"].append(query)
        return [{"id": f"qloo:{query}", "name": query, "type": "urn:entity:artist"}]

    async def fake_qloo(ids, discovery_level):
        return [{"id": "qloo-artist", "name": "Test Artist", "affinity": 0.9}]

    async def fake_plan(*args, **kwargs):
        calls["plan"].append(args[1])
        return SimpleNamespace(queries=["Test Artist", "minimalist painting"])

    review_calls = 0
    async def fake_review(*args, **kwargs):
        nonlocal review_calls
        review_calls += 1
        calls["review"].append(args[1])
        if review_calls == 1:
            return SimpleNamespace(
                assessments=[SimpleNamespace(artwork_id="commercial-1", score=92, fit_reason="Strong brief fit.", concern="")],
                follow_up_queries=["minimalist portrait"], critique="The initial pool is too narrow.", coverage="More variety needed.",
            )
        return SimpleNamespace(
            assessments=[
                SimpleNamespace(artwork_id="commercial-1", score=92, fit_reason="Strong brief fit.", concern=""),
                SimpleNamespace(artwork_id="institution-1", score=80, fit_reason="Good cultural fit.", concern="Not purchasable."),
            ],
            follow_up_queries=[], critique="The revised pool covers the brief.", coverage="Good.",
        )

    commercial = FakeProvider("Commercial", {
        "Test Artist": [work("commercial-1", "Test Artist", "commercial", "available", 3200)],
        "minimalist painting": [],
        "minimalist portrait": [],
    })
    institutional = FakeProvider("Institution", {
        "Test Artist": [work("institution-1", "Test Artist")],
        "minimalist painting": [],
        "minimalist portrait": [work("institution-2", "Another Artist")],
    })

    original = {
        "extract_request_signals": agent.extract_request_signals,
        "search_entities": agent.search_entities,
        "get_artist_insights": agent.get_artist_insights,
        "plan_art_searches": agent.plan_art_searches,
        "review_candidates": agent.review_candidates,
        "commercial_providers": agent.commercial_providers,
        "institutional_providers": agent.institutional_providers,
    }
    agent.extract_request_signals = fake_extract
    agent.search_entities = fake_search
    agent.get_artist_insights = fake_qloo
    agent.plan_art_searches = fake_plan
    agent.review_candidates = fake_review
    agent.commercial_providers = lambda: [commercial]
    agent.institutional_providers = lambda: [institutional]

    try:
        request = ConciergeRequest(
            intent=Intent.buy,
            loves=[],
            goal="Find me something calm for my living room under $5,000, inspired by the design I like.",
            discovery_level=50,
        )
        result = await agent.run_concierge(request)
        assert calls["extract"] == [request.goal]
        assert "Issey Miyake" in calls["qloo_search"]
        assert "Radiohead" in calls["qloo_search"]
        assert result["results"][0]["id"] == "commercial-1"
        assert all(item["source_kind"] == "commercial" and item["availability"] == "available" for item in result["results"])
        assert result["critique"] == "The revised pool covers the brief."
        assert review_calls == 2
        print("agent smoke: PASS")
        print(f"qloo searches: {calls['qloo_search']}")
        print(f"review passes: {review_calls}")
        print(f"primary results: {[item['id'] for item in result['results']]}")
        print(f"not-for-sale: {[item['id'] for item in result['not_for_sale']]}")
    finally:
        for name, value in original.items():
            setattr(agent, name, value)


if __name__ == "__main__":
    asyncio.run(main())

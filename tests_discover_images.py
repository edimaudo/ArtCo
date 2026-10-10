from services.art_sources import ArtInstituteProvider, ClevelandProvider, MetProvider, _normalise_image_url, _usable_image_url


def test_image_url_normalization():
    assert _normalise_image_url("//images.example.org/art.jpg") == "https://images.example.org/art.jpg"
    assert _normalise_image_url("https://images.example.org/{?width,height}") == "https://images.example.org/?width=900&height=1125"
    assert _normalise_image_url("javascript:alert(1)") is None
    assert _usable_image_url("https://images.example.org/art.jpg")
    assert not _usable_image_url(None)


def test_institutional_adapters_create_image_urls():
    aic = ArtInstituteProvider()._map(
        {"id": 12, "title": "Test painting", "artist_display": "Test Artist", "image_id": "abc-123", "is_public_domain": True},
        "https://www.artic.edu/iiif/2",
    )
    met = MetProvider()._map({"objectID": 22, "title": "Test sculpture", "primaryImageSmall": "https://images.metmuseum.org/test.jpg"})
    cleveland = ClevelandProvider()._map({"id": 33, "title": "Test print", "images": {"web": {"url": "https://openaccess-cdn.clevelandart.org/test.jpg"}}})
    assert aic.image_url == "https://www.artic.edu/iiif/2/abc-123/full/843,/0/default.jpg"
    assert met.image_url == "https://images.metmuseum.org/test.jpg"
    assert cleveland.image_url == "https://openaccess-cdn.clevelandart.org/test.jpg"
    assert all(_usable_image_url(work.image_url) for work in (aic, met, cleveland))

import asyncio
from types import SimpleNamespace

from models.schemas import ConciergeRequest, Intent
import services.agent as agent
from services.art_sources import Artwork


class FakeProvider:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def search(self, query: str, limit: int = 8):
        self.calls.append(query)
        return [
            Artwork(
                id=f"{self.name}:{index}", title=f"{self.name} work {index}", artist=f"Artist {self.name} {index}",
                image_url=f"https://images.example.org/{self.name.lower().replace(' ', '-')}-{index}.jpg",
                detail_url=f"https://collection.example.org/{self.name}/{index}", source=self.name,
                source_kind="institution", availability="not_for_sale", price=None, currency=None,
                medium="painting", dimensions="50 x 70 cm", year="2024", description="Open collection work",
            )
            for index in range(min(limit, 5))
        ]


def test_discover_returns_image_cards_and_separate_not_for_sale_works():
    async def run_test():
        providers = [FakeProvider("Collection A"), FakeProvider("Collection B"), FakeProvider("Collection C")]
        original = {
            "extract_request_signals": agent.extract_request_signals,
            "search_entities": agent.search_entities,
            "get_artist_insights": agent.get_artist_insights,
            "plan_art_searches": agent.plan_art_searches,
            "review_candidates": agent.review_candidates,
            "commercial_providers": agent.commercial_providers,
            "institutional_providers": agent.institutional_providers,
        }

        async def fake_extract(*args, **kwargs):
            return SimpleNamespace(
                cultural_references=[], art_interests=[], mediums=[], room=None, size_preference=None,
                market=None, budget_min=None, budget_max=None, number_of_works=1,
            )

        async def fake_search(query):
            return []

        async def fake_qloo(*args, **kwargs):
            return []

        async def fake_plan(*args, **kwargs):
            return SimpleNamespace(queries=["modern abstract painting", "contemporary artwork", "figurative painting", "mixed media art"])

        async def fake_review(*args, **kwargs):
            candidates = args[2]
            return SimpleNamespace(assessments=[], follow_up_queries=[], critique="Reviewed image-bearing works.", coverage="Good.")

        agent.extract_request_signals = fake_extract
        agent.search_entities = fake_search
        agent.get_artist_insights = fake_qloo
        agent.plan_art_searches = fake_plan
        agent.review_candidates = fake_review
        agent.commercial_providers = lambda: []
        agent.institutional_providers = lambda: providers
        try:
            result = await agent.run_concierge(ConciergeRequest(intent=Intent.discover, goal="Show me modern art", discovery_level=50))
            assert len(result["results"]) == 6
            assert len(result["not_for_sale"]) == 4
            assert all(item["image_url"].startswith("https://") for item in result["results"] + result["not_for_sale"])
            assert all(item["availability"] == "not_for_sale" for item in result["not_for_sale"])
            assert all(len(provider.calls) == 1 for provider in providers), "each provider should receive only one query in the first pass"
        finally:
            for name, value in original.items():
                setattr(agent, name, value)

    asyncio.run(run_test())

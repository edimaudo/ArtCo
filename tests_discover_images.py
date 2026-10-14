from services.art_sources import ArtInstituteProvider, ArtlogicProvider, ClevelandProvider, MetProvider, _map_artlogic, _normalise_image_url, _usable_image_url


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


def test_discover_returns_three_total_image_cards_not_a_second_shortlist():
    async def run_test():
        providers = [FakeProvider("Collection A"), FakeProvider("Collection B"), FakeProvider("Collection C")]
        original = {
            "extract_request_signals": agent.extract_request_signals,
            "search_entities": agent.search_entities,
            "get_artist_insights": agent.get_artist_insights,
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

        agent.extract_request_signals = fake_extract
        agent.search_entities = fake_search
        agent.get_artist_insights = fake_qloo
        agent.commercial_providers = lambda: []
        agent.institutional_providers = lambda: providers
        try:
            result = await agent.run_concierge(ConciergeRequest(intent=Intent.discover, goal="Show me modern art", discovery_level=50))
            assert len(result["results"]) == 3
            assert result["not_for_sale"] == []
            visible = result["results"] + result["not_for_sale"]
            assert len(visible) == 3
            assert all(item["image_url"].startswith("https://") for item in visible)
            assert all(len(provider.calls) == 1 for provider in providers), "each provider should receive only one query in the first pass"
        finally:
            for name, value in original.items():
                setattr(agent, name, value)

    asyncio.run(run_test())


def test_artwork_with_approved_source_page_can_be_previewed_without_api_thumbnail():
    from services.agent import _has_image_candidate
    work = ArtInstituteProvider()._map(
        {"id": 4321, "title": "Image metadata fallback", "artist_display": "Test Artist", "image_id": None},
        "https://www.artic.edu/iiif/2",
    )
    assert work.image_url is None
    assert work.detail_url == "https://www.artic.edu/artworks/4321"
    assert _has_image_candidate(work)


def test_artlogic_feed_maps_available_image_urls_from_common_fields():
    works = _map_artlogic({"rows": [
        {"id": 1, "title": "Direct thumb", "thumbnail_url": "https://cdn.gallery.example/work-1.jpg", "url": "https://gallery.example/work-1"},
        {"id": 2, "title": "Nested image", "images": {"web": {"url": "https://cdn.gallery.example/work-2.jpg"}}, "url": "https://gallery.example/work-2"},
        {"id": 3, "title": "Image list", "images": [{"src": "https://cdn.gallery.example/work-3.webp"}], "url": "https://gallery.example/work-3"},
    ]})
    assert [work.image_url for work in works] == [
        "https://cdn.gallery.example/work-1.jpg",
        "https://cdn.gallery.example/work-2.jpg",
        "https://cdn.gallery.example/work-3.webp",
    ]


def test_concierge_does_not_wait_for_slow_optional_enrichment(monkeypatch):
    import time
    from services.llm import RequestSignals

    provider = FakeProvider("Fast collection")

    async def slow_extract(*_args, **_kwargs):
        await asyncio.sleep(4)
        return RequestSignals()

    async def slow_qloo_search(_query):
        await asyncio.sleep(4)
        return []

    async def slow_insights(*_args, **_kwargs):
        await asyncio.sleep(4)
        return []

    monkeypatch.setattr(agent, "extract_request_signals", slow_extract)
    monkeypatch.setattr(agent, "search_entities", slow_qloo_search)
    monkeypatch.setattr(agent, "get_artist_insights", slow_insights)
    monkeypatch.setattr(agent, "commercial_providers", lambda: [])
    monkeypatch.setattr(agent, "institutional_providers", lambda: [provider])

    async def run_test():
        start = time.perf_counter()
        result = await agent.run_concierge(ConciergeRequest(intent=Intent.discover, goal="Find calm abstract art"))
        elapsed = time.perf_counter() - start
        assert elapsed < 1.2, f"fast catalogue results were blocked by enrichment ({elapsed:.2f}s)"
        assert len(result["results"]) <= 3
        assert result["diagnostics"]["duration_ms"] < 1200

    asyncio.run(run_test())


def test_concierge_caps_slow_catalogue_requests_under_five_seconds(monkeypatch):
    import time
    from services.llm import RequestSignals

    class SlowProvider:
        name = "Slow collection"
        async def search(self, query: str, limit: int = 8):
            await asyncio.sleep(8)
            return []

    async def fast_extract(*_args, **_kwargs):
        return RequestSignals()

    async def empty_search(_query):
        return []

    async def empty_insights(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent, "extract_request_signals", fast_extract)
    monkeypatch.setattr(agent, "search_entities", empty_search)
    monkeypatch.setattr(agent, "get_artist_insights", empty_insights)
    monkeypatch.setattr(agent, "commercial_providers", lambda: [])
    monkeypatch.setattr(agent, "institutional_providers", lambda: [SlowProvider()])

    async def run_test():
        start = time.perf_counter()
        result = await agent.run_concierge(ConciergeRequest(intent=Intent.discover, goal="Slow provider test"))
        elapsed = time.perf_counter() - start
        assert elapsed < 4.5, f"catalogue timeout exceeded the five-second service target ({elapsed:.2f}s)"
        assert result["diagnostics"]["duration_ms"] < 4500

    asyncio.run(run_test())

from fastapi.testclient import TestClient

from main import app
from services.image_proxy import display_image_url, is_allowed_detail_url, is_allowed_image_url


def _valid_jpeg_bytes():
    from io import BytesIO
    from PIL import Image
    stream = BytesIO()
    Image.new("RGB", (120, 160), (80, 110, 150)).save(stream, format="JPEG")
    return stream.getvalue()


def test_image_proxy_accepts_approved_art_sources():
    assert is_allowed_image_url("https://www.artic.edu/iiif/2/abc/full/843,/0/default.jpg")
    assert is_allowed_image_url("https://images.metmuseum.org/CRDImages/ep/original/example.jpg")
    assert is_allowed_image_url("https://openaccess-cdn.clevelandart.org/123/example.jpg")
    assert is_allowed_image_url("https://iiif.micr.io/example/full/800,/0/default.jpg")


def test_image_proxy_rejects_unsafe_urls_and_ports():
    assert not is_allowed_image_url("http://www.artic.edu/example.jpg")
    assert not is_allowed_image_url("https://127.0.0.1/private.jpg")
    assert not is_allowed_image_url("https://www.artic.edu:8443/example.jpg")
    assert not is_allowed_image_url("https://www.artic.edu.evil.example/image.jpg")
    assert not is_allowed_image_url("https://example.com/image.jpg")


def test_display_url_proxies_allowlisted_hosts_and_preserves_unknown_hosts():
    approved = "https://www.artic.edu/iiif/2/a-image/full/843,/0/default.jpg"
    display = display_image_url(approved)
    assert display and display.startswith("/api/artwork-image?url=")
    assert "https%3A%2F%2Fwww.artic.edu" in display
    unknown = "https://seller.example/image.jpg"
    assert display_image_url(unknown) == unknown


def test_proxy_endpoint_rejects_unapproved_host_without_fetching():
    client = TestClient(app)
    response = client.get("/api/artwork-image", params={"url": "https://example.com/image.jpg"})
    assert response.status_code == 400



def test_artwork_detail_page_allowlist_rejects_external_or_insecure_pages():
    assert is_allowed_detail_url("https://www.artic.edu/artworks/123")
    assert is_allowed_detail_url("https://www.metmuseum.org/art/collection/search/123")
    assert is_allowed_detail_url("https://www.clevelandart.org/art/123")
    assert not is_allowed_detail_url("https://www.rijksmuseum.nl/en/collection/123")
    assert is_allowed_detail_url("https://www.artsy.net/artwork/example")
    assert not is_allowed_detail_url("http://www.artic.edu/artworks/123")
    assert not is_allowed_detail_url("https://www.artic.edu.evil.example/artworks/123")
    assert not is_allowed_detail_url("https://example.com/artwork")


def test_artwork_preview_route_reads_open_graph_image(monkeypatch):
    import asyncio
    import httpx
    import main as app_main

    page_url = "https://www.artic.edu/artworks/123"
    image_url = "https://www.artic.edu/iiif/2/preview-123/full/843,/0/default.jpg"
    page_response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=f'<html><head><meta property="og:image" content="{image_url}"></head></html>'.encode(),
        request=httpx.Request("GET", page_url),
    )
    image_bytes = _valid_jpeg_bytes()
    image_response = httpx.Response(
        200,
        headers={"content-type": "image/jpeg"},
        content=image_bytes,
        request=httpx.Request("GET", image_url),
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.responses = {page_url: page_response, image_url: image_response}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, url, **kwargs):
            return self.responses[url]

    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeClient)
    from fastapi.testclient import TestClient
    client = TestClient(app_main.app)
    response = client.get("/api/artwork-preview", params={"url": page_url})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == image_bytes


def test_concierge_markup_omits_redundant_image_storage_copy_and_limit():
    from fastapi.testclient import TestClient
    from main import app
    response = TestClient(app).get("/concierge")
    assert response.status_code == 200
    assert "Images are stored in this browser" not in response.text
    assert "Optional · Up to 5 per request" not in response.text
    assert 'id="inspirationFiles"' in response.text


def test_results_cap_recommendations_to_three_and_keep_three_column_desktop_grid():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    js = (root / "static/js/app.js").read_text()
    css = (root / "static/css/style.css").read_text()
    agent = (root / "services/agent.py").read_text()
    concierge = (root / "templates/concierge.html").read_text()
    assert "const purchaseOnlyIntent = ['buy', 'curate', 'taste'].includes(intent) || usesSavedTaste;" in js
    assert "work.source_kind === 'commercial' && work.availability === 'available'" in js
    assert ".slice(0, 3)" in js
    assert "const notForSale = [];" in js
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "primary = selected[:3]" in agent
    assert "secondary_limit = 0" in agent
    assert '"not_for_sale": [] if purchase_only_intent' in agent
    assert "width: min(100%, 280px)" in css
    assert "Images are stored in this browser" not in concierge
    assert "Optional · Up to 5 per request" not in concierge
    assert 'referrerpolicy="no-referrer"' in js
    assert "const detailUrl = image.dataset.detailUrl" in js
    assert "const originalSrc = image.dataset.originalSrc" in js


def test_inspiration_images_are_memory_only_and_old_browser_store_is_migrated_away():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    js = (root / "static/js/app.js").read_text()
    assert "window.indexedDB.open(INSPIRATION_DB_NAME, 3)" in js
    assert "db.deleteObjectStore(INSPIRATION_STORE)" in js
    assert "objectStore(INSPIRATION_STORE).put(record)" not in js
    assert "function clearCurrentInspirationImages()" in js
    assert "delete historyRequest.inspiration_images" in js
    assert "localStorage.setItem(storage.selectedInspiration" not in js


def test_artwork_preview_route_recovers_from_img_src_when_og_image_is_missing(monkeypatch):
    import httpx
    import main as app_main

    page_url = "https://www.metmuseum.org/art/collection/search/123"
    image_url = "https://images.metmuseum.org/CRDImages/test/primary.jpg"
    page_response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=(f'<html><body><img class="object-image" alt="Artwork image" '
                 f'src="http://images.metmuseum.org/CRDImages/test/primary.jpg" width="900" height="1200"></body></html>').encode(),
        request=httpx.Request("GET", page_url),
    )
    image_response = httpx.Response(
        200,
        headers={"content-type": "image/jpeg"},
        content=_valid_jpeg_bytes(),
        request=httpx.Request("GET", image_url),
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.responses = {page_url: page_response, image_url: image_response}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, url, **kwargs):
            return self.responses[url]

    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeClient)
    response = TestClient(app_main.app).get("/api/artwork-preview", params={"url": page_url})
    assert response.status_code == 200
    assert response.content == _valid_jpeg_bytes()


def test_artsy_collection_pages_are_eligible_for_safe_image_fallback():
    from services.image_proxy import is_allowed_detail_url
    assert is_allowed_detail_url("https://www.artsy.net/artwork/example")
    assert not is_allowed_detail_url("https://artsy.net.evil.example/artwork/example")


def test_proxy_recognizes_valid_jpeg_bytes_with_generic_content_type():
    from main import _sniff_image_media_type
    assert _sniff_image_media_type("application/octet-stream", _valid_jpeg_bytes()) == "image/jpeg"
    assert _sniff_image_media_type("application/octet-stream", b"<html>not an image</html>") is None


def test_art_source_http_image_url_is_upgraded_to_avoid_mixed_content():
    from services.art_sources import _normalise_image_url
    assert _normalise_image_url("http://images.metmuseum.org/object.jpg") == "https://images.metmuseum.org/object.jpg"
    assert _normalise_image_url("http://untrusted.example/object.jpg") == "https://untrusted.example/object.jpg"


def test_proxy_accepts_common_gallery_cdn_hosts():
    assert is_allowed_image_url("https://res.cloudinary.com/gallery/image/upload/work.jpg")
    assert is_allowed_image_url("https://images.squarespace-cdn.com/content/v1/work.jpg")
    assert is_allowed_image_url("https://cdn.shopify.com/s/files/1/work.jpg")
    assert is_allowed_image_url("https://images.ctfassets.net/site/work.jpg")


def test_concierge_api_caps_all_visible_artwork_cards_to_three(monkeypatch):
    import main as app_main
    from models.schemas import ConciergeRequest, Intent

    async def fake_run(payload):
        return {
            "success": True,
            "intent": payload.intent.value,
            "results": [{"id": f"primary-{i}"} for i in range(8)],
            "not_for_sale": [{"id": f"secondary-{i}"} for i in range(8)],
        }

    monkeypatch.setattr(app_main, "run", fake_run)
    client = TestClient(app_main.app)
    response = client.post("/api/concierge", json={"intent": "discover", "goal": "show me art"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 3
    assert data["not_for_sale"] == []

    response = client.post("/api/concierge", json={"intent": "buy", "goal": "show me art"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 3
    assert data["not_for_sale"] == []


def test_static_assets_are_versioned_to_avoid_stale_result_caps():
    from pathlib import Path
    html = (Path(__file__).resolve().parent / "templates/base.html").read_text()
    assert "/static/js/app.js?v=20261022-learn-artist-guide" in html
    assert "/static/css/style.css?v=20261022-learn-artist-guide" in html


def test_artlogic_cloudfront_cdn_is_proxyable():
    assert is_allowed_image_url("https://d7hftxdivxxvm.cloudfront.net/?resize_to=fit&width=900")


def test_concierge_route_enforces_a_hard_response_deadline(monkeypatch):
    import asyncio
    import main as app_main
    from fastapi.testclient import TestClient

    async def slow_run(payload):
        await asyncio.sleep(0.25)
        return {"success": True, "intent": payload.intent.value, "results": []}

    monkeypatch.setattr(app_main, "run", slow_run)
    monkeypatch.setattr(app_main, "CONCIERGE_RESPONSE_TIMEOUT_SECONDS", 0.05)
    response = TestClient(app_main.app).post(
        "/api/concierge", json={"intent": "discover", "goal": "modern painting"}
    )
    assert response.status_code == 504
    assert response.json()["error_type"] == "ResponseDeadlineExceeded"


def test_result_count_copy_reports_visible_recommendations_not_research_pool_size():
    from pathlib import Path
    js = (Path(__file__).resolve().parent / "static/js/app.js").read_text()
    assert "`${visibleCount} recommendation${visibleCount === 1 ? '' : 's'} shown`" in js
    assert "${data.total_found || 0} works researched" not in js


def test_collect24_gallery_pages_and_images_are_eligible_for_safe_preview():
    assert is_allowed_image_url("https://cdn.collect24.art/artwork/example.jpg")
    assert is_allowed_image_url("https://images.collect24.art/artwork/example.jpg")
    assert is_allowed_detail_url("https://www.collect24.art/artwork/example")


def test_saved_more_like_action_starts_a_new_request():
    from pathlib import Path
    js = (Path(__file__).resolve().parent / "static/js/app.js").read_text()
    assert "function beginSimilarSearch(artwork)" in js
    assert "window.ART_CONCIERGE_PAGE === 'saved' && /^More like/i.test(text)" in js
    assert "window.location.assign(resultsRunUrl('discover'))" in js
    assert "request.inspiration_image_ids = []" in js


def test_saved_artwork_preview_fallback_prefers_direct_and_exact_work_before_source_page():
    from pathlib import Path
    js = (Path(__file__).resolve().parent / "static/js/app.js").read_text()
    start = js.index("function bindArtworkImageFallbacks(root)")
    end = js.index("function artworkPreviewUrl(detailUrl)", start)
    fallback = js[start:end]
    assert fallback.index("image.src = originalSrc") < fallback.index("image.src = artworkFallbackUrl") < fallback.index("image.src = `/api/artwork-preview?url=")


def test_qloo_ignores_avoid_references_and_visual_subjects(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import services.agent as agent
    from models.schemas import ConciergeRequest, Intent

    async def check():
        queries = []
        async def fake_search(query):
            queries.append(query)
            return [{"id": f"entity:{query}", "name": query}]
        monkeypatch.setattr(agent, "search_entities", fake_search)
        request = ConciergeRequest(
            intent=Intent.taste,
            loves=["Radiohead"],
            avoid_references=["Beyonce"],
            art_interests=["birds", "watercolour painting"],
            additional_interests=["Issey Miyake"],
            goal="Develop my taste",
        )
        signals = SimpleNamespace(cultural_references=["Beyonce", "Radiohead"], art_interests=["birds"])
        await agent._resolve_entities(request, [], signals)
        assert queries == ["Radiohead", "Issey Miyake"]

    asyncio.run(check())


def test_cultural_art_directions_only_boost_matching_work_metadata():
    from models.schemas import ConciergeRequest, Intent
    from services.agent import _deterministic_score
    from services.art_sources import Artwork

    request = ConciergeRequest(intent=Intent.discover, goal="Explore art", discovery_level=50)
    work = Artwork(
        id="test:composition", title="Experimental composition", artist="A. Example",
        image_url="https://example.com/art.jpg", detail_url="https://example.com/work",
        source="Test collection", source_kind="institution", availability="not_for_sale",
        price=None, currency=None, medium="mixed media", dimensions=None, year=None,
        description="Layered, expressive forms.",
    )
    baseline = _deterministic_score(work, request, [], "contemporary art")
    culturally_relevant = _deterministic_score(
        work, request, [], "contemporary art", ["experimental composition", "expressive forms"]
    )
    assert culturally_relevant > baseline


def test_image_attached_concierge_uses_the_longer_required_image_search_deadline(monkeypatch):
    import asyncio
    import main as app_main
    from fastapi.testclient import TestClient

    observed = {}

    async def slow_run(payload):
        observed["image_count"] = len(payload.inspiration_images)
        await asyncio.sleep(0.12)
        return {"success": True, "intent": payload.intent.value, "results": []}

    monkeypatch.setattr(app_main, "run", slow_run)
    monkeypatch.setattr(app_main, "CONCIERGE_IMAGE_RESPONSE_TIMEOUT_SECONDS", 0.5)
    response = TestClient(app_main.app).post(
        "/api/concierge", json={
            "intent": "discover",
            "goal": "modern painting",
            "inspiration_image_ids": ["image-1"],
            "inspiration_images": [{
                "image_id": "image-1", "mime_type": "image/jpeg", "data": "YXJ0" + "eA" * 20, 
            }],
        }
    )
    assert response.status_code == 200
    assert observed["image_count"] == 1


def test_required_image_search_failure_is_returned_as_service_error(monkeypatch):
    import main as app_main
    from fastapi.testclient import TestClient

    async def failed_run(payload):
        return {
            "success": False,
            "error_type": "RequiredVisualSearchFailed",
            "detail": "Image interpretation is required.",
            "results": [],
        }

    monkeypatch.setattr(app_main, "run", failed_run)
    response = TestClient(app_main.app).post(
        "/api/concierge", json={
            "intent": "discover",
            "goal": "modern painting",
            "inspiration_image_ids": ["image-1"],
            "inspiration_images": [{"image_id": "image-1", "mime_type": "image/jpeg", "data": "YXJ0" + "eA" * 20}],
        }
    )
    assert response.status_code == 503
    assert response.json()["error_type"] == "RequiredVisualSearchFailed"


def test_artwork_preview_skips_failed_metadata_image_and_tries_next_candidate(monkeypatch):
    import httpx
    import main as app_main

    page_url = "https://www.artic.edu/artworks/456"
    broken_url = "https://www.artic.edu/iiif/2/broken/full/843,/0/default.jpg"
    good_url = "https://www.artic.edu/iiif/2/good/full/843,/0/default.jpg"
    page_response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=(f'<html><head><meta property="og:image" content="{broken_url}"></head>'
                 f'<body><img class="artwork-image" alt="Artwork" src="{good_url}"></body></html>').encode(),
        request=httpx.Request("GET", page_url),
    )
    broken_response = httpx.Response(403, request=httpx.Request("GET", broken_url))
    recovered_bytes = _valid_jpeg_bytes()
    good_response = httpx.Response(
        200, headers={"content-type": "image/jpeg"}, content=recovered_bytes,
        request=httpx.Request("GET", good_url),
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.responses = {page_url: page_response, broken_url: broken_response, good_url: good_response}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, **kwargs): return self.responses[url]

    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeClient)
    response = TestClient(app_main.app).get("/api/artwork-preview", params={"url": page_url})
    assert response.status_code == 200
    assert response.content == recovered_bytes


def test_artwork_image_fallback_searches_for_the_same_work(monkeypatch):
    import services.art_sources as sources
    from services.art_sources import Artwork

    work = Artwork(
        id="fallback-work", title="Blue Study", artist="Morgan Lee",
        image_url="https://www.artic.edu/iiif/2/fallback/full/843,/0/default.jpg",
        detail_url="https://www.artic.edu/artworks/456", source="Art Institute of Chicago",
        source_kind="institution", availability="not_for_sale", price=None, currency=None,
        medium="oil on canvas", dimensions=None, year="2020", description=None,
    )

    class FakeProvider:
        def __init__(self, *args, **kwargs): pass
        async def search(self, query, limit=4): return [work]

    monkeypatch.setattr(sources, "ArtInstituteProvider", FakeProvider)
    monkeypatch.setattr(sources, "MetProvider", FakeProvider)
    monkeypatch.setattr(sources, "ClevelandProvider", FakeProvider)
    response = TestClient(app).get("/api/artwork-image-fallback", params={"title": "Blue Study", "artist": "Morgan Lee"}, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].startswith("/api/artwork-image?url=")


def test_recommendation_markup_has_no_preview_unavailable_fallback_copy():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    js = (root / "static/js/app.js").read_text()
    assert "Preview unavailable" not in js
    assert "/api/artwork-image-fallback?" in js
    assert "data-artwork-title" in js


def test_image_proxy_rejects_html_or_svg_placeholders_even_when_declared_as_images():
    from main import _sniff_image_media_type
    assert _sniff_image_media_type("image/jpeg", b"<html><body>denied</body></html>" + b" " * 64) is None
    assert _sniff_image_media_type("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>' + b" " * 64) is None


def test_non_north_american_museum_hosts_are_no_longer_allowlisted():
    assert not is_allowed_image_url("https://ids.si.edu/ids/deliveryService?id=NPG-NPG_123")
    assert not is_allowed_image_url("https://www.nationalgallery.org.uk/media/example.jpg")
    assert not is_allowed_detail_url("https://www.nationalgallery.org.uk/paintings")
    assert not is_allowed_detail_url("https://americanart.si.edu/artwork/example")


def test_artsy_adapter_maps_large_image_and_rejects_missing_thumbnail():
    from services.art_sources import _map_artsy
    artwork = _map_artsy({
        "id": "work-1", "title": "Bloom", "can_acquire": True,
        "_embedded": {"artists": [{"name": "Yayoi Kusama"}]},
        "_links": {"thumbnail": {"href": "https://d32dm0rphc51dk.cloudfront.net/abc/square.jpg"},
                   "permalink": {"href": "https://www.artsy.net/artwork/work-1"}},
    })
    missing = _map_artsy({"id": "work-2", "title": "Missing", "_links": {"thumbnail": {"href": "https://d32dm0rphc51dk.cloudfront.net/assets/shared/missing_image.png"}}})
    assert artwork.image_url == "https://d32dm0rphc51dk.cloudfront.net/abc/large.jpg"
    assert missing.image_url is None

def test_artwork_dedupes_across_provider_specific_ids():
    from services.agent import _dedupe_artworks
    from services.art_sources import Artwork
    works = [
        Artwork(id="met:1", title="Tsumari in Bloom", artist="Yayoi Kusama", image_url="https://images.metmuseum.org/a.jpg", detail_url="https://www.metmuseum.org/a", source="Met", source_kind="institution", availability="not_for_sale", price=None, currency=None, medium="painting", dimensions=None, year="2020", description=None),
        Artwork(id="aic:2", title="Tsumari in Bloom", artist="Yayoi Kusama", image_url="https://www.artic.edu/iiif/2/b/full/843,/0/default.jpg", detail_url="https://www.artic.edu/artworks/2", source="Art Institute of Chicago", source_kind="institution", availability="not_for_sale", price=None, currency=None, medium="painting", dimensions=None, year="2020", description=None),
        Artwork(id="met:3", title="Other Work", artist="Yayoi Kusama", image_url="https://images.metmuseum.org/c.jpg", detail_url="https://www.metmuseum.org/c", source="Met", source_kind="institution", availability="not_for_sale", price=None, currency=None, medium="painting", dimensions=None, year="2020", description=None),
    ]
    assert [work.id for work in _dedupe_artworks(works)] == ["met:1", "met:3"]

def test_institutional_sources_are_limited_to_north_american_fallback_sources():
    from services.art_sources import institutional_providers
    providers = institutional_providers()
    names = [provider.name for provider in providers]
    assert names == ["Art Institute of Chicago", "The Metropolitan Museum of Art", "Cleveland Museum of Art"]
    assert all(getattr(provider, "country_code", None) == "US" for provider in providers)
    assert not any("Smithsonian" in name or "Rijksmuseum" in name or "National Gallery, London" in name for name in names)
    from services.config import ARTWORK_RESULT_LIMIT
    assert ARTWORK_RESULT_LIMIT <= 3


def test_page_preview_prefers_real_inline_art_over_generic_og_image(monkeypatch):
    import httpx
    import main as app_main
    page_url = "https://www.artic.edu/artworks/789"
    logo_url = "https://www.artic.edu/iiif/2/site-logo/full/843,/0/default.jpg"
    art_url = "https://www.artic.edu/iiif/2/real-art/full/843,/0/default.jpg"
    page_response = httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
        content=(f'<html><head><meta property="og:image" content="{logo_url}"></head><body>'
                 f'<img alt="Artwork image" width="900" height="1200" src="{art_url}"></body></html>').encode(),
        request=httpx.Request("GET", page_url))
    art_bytes = _valid_jpeg_bytes()
    art_response = httpx.Response(200, headers={"content-type": "image/jpeg"}, content=art_bytes, request=httpx.Request("GET", art_url))
    calls = []
    class FakeClient:
        def __init__(self, *args, **kwargs): self.responses = {page_url: page_response, art_url: art_response, logo_url: httpx.Response(200, headers={"content-type":"image/jpeg"}, content=_valid_jpeg_bytes(), request=httpx.Request("GET", logo_url))}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, **kwargs): calls.append(url); return self.responses[url]
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeClient)
    response = TestClient(app_main.app).get("/api/artwork-preview", params={"url": page_url})
    assert response.status_code == 200
    assert response.content == art_bytes
    assert art_url in calls
    assert logo_url not in calls


def test_artsy_search_uses_configured_partner_inventory_not_retiring_public_search(monkeypatch):
    import httpx
    import services.art_sources as sources
    monkeypatch.setattr(sources, "ARTSY_XAPP_TOKEN", "test-token")
    monkeypatch.setattr(sources, "ARTSY_PARTNER_ID", "partner-123")
    request_seen = {}
    payload = {"_embedded": {"artworks": [
        {"id": "a1", "title": "Red Bloom", "can_acquire": True, "sold": False,
         "_embedded": {"artists": [{"name": "Yayoi Kusama"}]},
         "_links": {"thumbnail": {"href": "https://d32dm0rphc51dk.cloudfront.net/a1/medium.jpg"}, "permalink": {"href": "https://www.artsy.net/artwork/a1"}}},
        {"id": "a2", "title": "Blue Garden", "can_acquire": True, "sold": False,
         "_embedded": {"artists": [{"name": "Another Artist"}]},
         "_links": {"thumbnail": {"href": "https://d32dm0rphc51dk.cloudfront.net/a2/medium.jpg"}, "permalink": {"href": "https://www.artsy.net/artwork/a2"}}},
    ]}}
    class FakeClient:
        def __init__(self, *args, **kwargs): request_seen["headers"] = kwargs.get("headers")
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, **kwargs):
            request_seen["url"] = url
            request_seen["params"] = kwargs.get("params")
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
    monkeypatch.setattr(sources.httpx, "AsyncClient", FakeClient)
    import asyncio
    works = asyncio.run(sources.ArtsyProvider().search("Yayoi Kusama", limit=6))
    assert len(works) == 1
    assert works[0].artist == "Yayoi Kusama"
    assert request_seen["url"].endswith("/artworks")
    assert request_seen["params"]["partner_id"] == "partner-123"
    assert request_seen["params"]["published"] == "true"


def test_deduplication_preserves_distinct_untitled_works_by_same_artist():
    from services.agent import _dedupe_artworks
    from services.art_sources import Artwork
    base = dict(artist="Yayoi Kusama", detail_url="https://www.artsy.net/artwork/example", source="Artsy", source_kind="commercial", availability="available", price=None, currency=None, medium=None, dimensions=None, year=None, description=None)
    works = [
        Artwork(id="artsy:1", title="Untitled", image_url="https://d32dm0rphc51dk.cloudfront.net/one/large.jpg", **base),
        Artwork(id="artsy:2", title="Untitled", image_url="https://d32dm0rphc51dk.cloudfront.net/two/large.jpg", **base),
        Artwork(id="artsy:3", title="Tsumari in Bloom", image_url="https://d32dm0rphc51dk.cloudfront.net/three/large.jpg", **base),
        Artwork(id="met:4", title="Tsumari in Bloom", image_url="https://images.metmuseum.org/three.jpg", **{**base, "source": "Met", "source_kind": "institution", "availability": "not_for_sale"}),
    ]
    assert [work.id for work in _dedupe_artworks(works)] == ["artsy:1", "artsy:2", "artsy:3"]

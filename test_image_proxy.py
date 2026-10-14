from fastapi.testclient import TestClient

from main import app
from services.image_proxy import display_image_url, is_allowed_detail_url, is_allowed_image_url


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
    assert is_allowed_detail_url("https://www.rijksmuseum.nl/en/collection/123")
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
    image_response = httpx.Response(
        200,
        headers={"content-type": "image/jpeg"},
        content=b"mock-jpeg-bytes",
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
    assert response.content == b"mock-jpeg-bytes"


def test_concierge_markup_omits_redundant_image_storage_copy_and_limit():
    from fastapi.testclient import TestClient
    from main import app
    response = TestClient(app).get("/concierge")
    assert response.status_code == 200
    assert "Images are stored in this browser" not in response.text
    assert "Optional · Up to 5 per request" not in response.text
    assert 'id="inspirationFiles"' in response.text


def test_results_cap_recommendations_to_three_and_use_three_column_desktop_grid():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    js = (root / "static/js/app.js").read_text()
    css = (root / "static/css/style.css").read_text()
    agent = (root / "services/agent.py").read_text()
    concierge = (root / "templates/concierge.html").read_text()
    assert "(data.results || []).slice(0, 3)" in js
    assert "Math.max(0, 3 - results.length)" in js
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "primary = selected[:3]" in agent
    assert 'secondary_limit = max(0, 3 - len(primary)) if request.intent.value == "buy" else 0' in agent
    assert "width: min(100%, 185px)" in css
    assert "Images are stored in this browser" not in concierge
    assert "Optional · Up to 5 per request" not in concierge
    assert 'referrerpolicy="no-referrer"' not in js
    assert "const detailUrl = image.dataset.detailUrl" in js
    assert "const originalSrc = image.dataset.originalSrc" in js


def test_inspiration_selected_ids_are_not_persisted_to_localstorage():
    from pathlib import Path
    js = (Path(__file__).resolve().parent / "static/js/app.js").read_text()
    assert "putInspirationSetting('selectedIds', state.inspiration.selectedIds)" in js
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
        content=b"mock-primary-image",
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
    assert response.content == b"mock-primary-image"


def test_artsy_collection_pages_are_eligible_for_safe_image_fallback():
    from services.image_proxy import is_allowed_detail_url
    assert is_allowed_detail_url("https://www.artsy.net/artwork/example")
    assert not is_allowed_detail_url("https://artsy.net.evil.example/artwork/example")


def test_proxy_recognizes_valid_jpeg_bytes_with_generic_content_type():
    from main import _sniff_image_media_type
    assert _sniff_image_media_type("application/octet-stream", b"\xff\xd8\xff\xe0jpeg") == "image/jpeg"
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
            "results": [{"id": f"primary-{i}"} for i in range(5)],
            "not_for_sale": [{"id": f"secondary-{i}"} for i in range(5)],
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
    assert "/static/js/app.js?v=20261013-2" in html
    assert "/static/css/style.css?v=20261013-2" in html


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

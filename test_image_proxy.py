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
    assert "(data.not_for_sale || []).slice(0, 3)" in js
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "primary = selected[:3]" in agent
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

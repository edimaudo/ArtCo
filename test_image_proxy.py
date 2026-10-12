from fastapi.testclient import TestClient

from main import app
from services.image_proxy import display_image_url, is_allowed_image_url


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

"""Safe image URL helpers for public collection artwork thumbnails.

Only known public art collection image hosts are eligible for proxying. This
keeps the endpoint from becoming an arbitrary URL fetcher/SSRF surface.
"""
from __future__ import annotations

from urllib.parse import quote, urlparse

ALLOWED_EXACT_HOSTS = {
    "images.metmuseum.org",
    "iiif.micr.io",
    "d32dm0rphc51dk.cloudfront.net",  # Artsy public thumbnails
    "d7hftxdivxxvm.cloudfront.net",  # Artlogic / hosted gallery thumbnails
    "cdn.shopify.com",
    "images.ctfassets.net",
    "cdn.collect24.art",
    "images.collect24.art",
    "media.collect24.art",
}
ALLOWED_HOST_SUFFIXES = (
    ".artic.edu",
    ".metmuseum.org",
    ".clevelandart.org",
    ".micr.io",
    ".cloudinary.com",
    ".shopifycdn.com",
    ".squarespace-cdn.com",
    ".ctfassets.net",
    ".imgix.net",
    ".artlogic.net",
    ".collect24.art",
)

ALLOWED_DETAIL_HOSTS = {
    "artic.edu", "www.artic.edu",
    "metmuseum.org", "www.metmuseum.org",
    "clevelandart.org", "www.clevelandart.org",
    "artsy.net", "www.artsy.net",
    "collect24.art", "www.collect24.art",
}
ALLOWED_DETAIL_HOST_SUFFIXES = (
    ".artic.edu", ".metmuseum.org", ".clevelandart.org", ".artsy.net", ".artlogic.net", ".collect24.art"
)


def _safe_https_url(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(str(value or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 443):
            return None
        return host, parsed.netloc
    except (TypeError, ValueError):
        return None


def is_allowed_detail_url(value: str) -> bool:
    """Restrict page-preview scraping to the official public collection sites."""
    parsed = _safe_https_url(value)
    if not parsed:
        return False
    host, _ = parsed
    return host in ALLOWED_DETAIL_HOSTS or any(host.endswith(suffix) for suffix in ALLOWED_DETAIL_HOST_SUFFIXES)


def is_allowed_image_url(value: str) -> bool:
    """Return True for HTTPS URLs on explicit public art-image hosts only."""
    parsed = _safe_https_url(value)
    if not parsed:
        return False
    host, _ = parsed
    # No non-standard ports. Hosts above are public art collection image hosts only.
    return host in ALLOWED_EXACT_HOSTS or any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def display_image_url(value: str | None) -> str | None:
    """Use the same-origin image proxy for approved hosts; otherwise keep source URL."""
    if not value:
        return None
    value = str(value).strip()
    if is_allowed_image_url(value):
        return f"/api/artwork-image?url={quote(value, safe='')}"
    return value

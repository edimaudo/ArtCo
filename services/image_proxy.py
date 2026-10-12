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
}
ALLOWED_HOST_SUFFIXES = (
    ".artic.edu",
    ".metmuseum.org",
    ".clevelandart.org",
    ".micr.io",
    ".rijksmuseum.nl",
)


def is_allowed_image_url(value: str) -> bool:
    """Return True for HTTPS URLs on explicit public art-image hosts only."""
    try:
        parsed = urlparse(str(value or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        # No non-standard ports. Hosts above are public CDNs/APIs only.
        if parsed.port not in (None, 443):
            return False
        return host in ALLOWED_EXACT_HOSTS or any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)
    except (TypeError, ValueError):
        return False


def display_image_url(value: str | None) -> str | None:
    """Use the same-origin image proxy for approved hosts; otherwise keep source URL."""
    if not value:
        return None
    value = str(value).strip()
    if is_allowed_image_url(value):
        return f"/api/artwork-image?url={quote(value, safe='')}"
    return value

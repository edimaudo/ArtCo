from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

import httpx

from .config import (
    ARTSY_PARTNER_ID,
    ARTSY_XAPP_TOKEN,
    ARTLOGIC_FEED_URL,
    ARTWORK_RESULT_LIMIT,
    COLLECT24_API_KEY,
    COLLECT24_BASE_URL,
    REQUEST_TIMEOUT,
)


@dataclass(slots=True)
class Artwork:
    id: str
    title: str
    artist: str
    image_url: str | None
    detail_url: str | None
    source: str
    source_kind: str
    availability: str
    price: float | None
    currency: str | None
    medium: str | None
    dimensions: str | None
    year: str | None
    description: str | None
    matched_direction: str | None = None
    purchase_mode: str = "external"
    seller_name: str | None = None


class ArtworkProvider(Protocol):
    name: str

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]: ...


class ArtInstituteProvider:
    name = "Art Institute of Chicago"
    base_url = "https://api.artic.edu/api/v1/artworks"

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]:
        # Search broadly enough to replace records with no usable reproduction,
        # then return only works with an actual image identifier.
        params = {
            "q": query,
            "limit": min(max(limit * 3, limit), 40),
            "fields": "id,title,artist_display,date_display,medium_display,dimensions,image_id,credit_line,is_public_domain",
            "query[term][is_public_domain]": "true",
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(f"{self.base_url}/search", params=params)
            response.raise_for_status()
            payload = response.json()
        iiif = payload.get("config", {}).get("iiif_url", "https://www.artic.edu/iiif/2")
        output = [self._map(item, iiif) for item in payload.get("data", [])]
        return [work for work in output if _usable_image_url(work.image_url)][:limit]

    def _map(self, item: dict[str, Any], iiif: str) -> Artwork:
        image_id = item.get("image_id")
        image_url = f"{iiif.rstrip('/')}/{image_id}/full/843,/0/default.jpg" if image_id else None
        return Artwork(
            id=f"aic:{item.get('id')}",
            title=item.get("title") or "Untitled",
            artist=item.get("artist_display") or "Unknown artist",
            image_url=image_url,
            detail_url=f"https://www.artic.edu/artworks/{item.get('id')}",
            source=self.name,
            source_kind="institution",
            availability="not_for_sale",
            price=None,
            currency=None,
            medium=item.get("medium_display"),
            dimensions=item.get("dimensions"),
            year=item.get("date_display"),
            description=item.get("credit_line"),
        )


class MetProvider:
    name = "The Metropolitan Museum of Art"
    base_url = "https://collectionapi.metmuseum.org/public/collection/v1.1"

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]:
        # Keep detail fan-out small; the Met search endpoint returns IDs, while
        # object details carry the image URLs needed by the gallery.
        requested = min(max(limit, 1), 5)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{self.base_url}/search",
                params={"q": query, "hasImages": "true", "limit": requested, "offset": 0},
            )
            response.raise_for_status()
            ids = (response.json().get("objectIDs") or [])[:requested]
            details = await asyncio.gather(
                *(client.get(f"{self.base_url}/objects/{object_id}") for object_id in ids),
                return_exceptions=True,
            )
        output: list[Artwork] = []
        for result in details:
            if not isinstance(result, httpx.Response) or result.status_code != 200:
                continue
            try:
                work = self._map(result.json())
            except (TypeError, ValueError):
                continue
            if _usable_image_url(work.image_url):
                output.append(work)
        return output[:limit]

    def _map(self, item: dict[str, Any]) -> Artwork:
        return Artwork(
            id=f"met:{item.get('objectID')}",
            title=item.get("title") or "Untitled",
            artist=item.get("artistDisplayName") or "Unknown artist",
            image_url=_normalise_image_url(item.get("primaryImageSmall") or item.get("primaryImage")),
            detail_url=item.get("objectURL"),
            source=self.name,
            source_kind="institution",
            availability="not_for_sale",
            price=None,
            currency=None,
            medium=item.get("medium"),
            dimensions=item.get("dimensions"),
            year=item.get("objectDate"),
            description=item.get("creditLine"),
        )


class ClevelandProvider:
    name = "Cleveland Museum of Art"
    base_url = "https://openaccess-api.clevelandart.org/api/artworks"

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(self.base_url, params={"q": query, "limit": min(max(limit * 3, limit), 40)})
            response.raise_for_status()
            payload = response.json()
        output = [self._map(item) for item in (payload.get("data") or []) if isinstance(item, dict)]
        return [work for work in output if _usable_image_url(work.image_url)][:limit]

    def _map(self, item: dict[str, Any]) -> Artwork:
        images = item.get("images") or {}
        image_url = None
        if isinstance(images, dict):
            for key in ("web", "print", "original"):
                value = images.get(key)
                if isinstance(value, dict):
                    image_url = _normalise_image_url(value.get("url") or value.get("href"))
                    if image_url:
                        break
                elif isinstance(value, str):
                    image_url = _normalise_image_url(value)
                    if image_url:
                        break
        return Artwork(
            id=f"cma:{item.get('id') or item.get('accession_number')}",
            title=item.get("title") or "Untitled",
            artist=item.get("artist") or item.get("creator") or "Unknown artist",
            image_url=image_url,
            detail_url=item.get("url"),
            source=self.name,
            source_kind="institution",
            availability="not_for_sale",
            price=None,
            currency=None,
            medium=item.get("technique") or item.get("medium"),
            dimensions=item.get("measurements"),
            year=str(item.get("creation_date")) if item.get("creation_date") else None,
            description=item.get("description"),
        )


class RijksmuseumProvider:
    name = "Rijksmuseum"
    base_url = "https://data.rijksmuseum.nl/search/collection"

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]:
        # Rijksmuseum search returns Linked Art identifiers, not image URLs.
        # Resolve only a very small fallback set through the documented
        # object -> VisualItem -> DigitalObject chain to keep search responsive.
        requested = min(max(limit, 1), 2)
        headers = {"Accept": "application/json"}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True) as client:
            response = await client.get(
                self.base_url,
                params={"description": query, "imageAvailable": "true", "type": "painting"},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("orderedItems") or []
            identifiers = [item.get("id") for item in items if isinstance(item, dict) and item.get("id")][:requested]
            works = await asyncio.gather(
                *(self._resolve_work(client, uri) for uri in identifiers),
                return_exceptions=True,
            )
        return [work for work in works if isinstance(work, Artwork) and _usable_image_url(work.image_url)][:limit]

    async def _get_linked_record(self, client: httpx.AsyncClient, uri: str) -> dict[str, Any]:
        parsed = urlparse(uri)
        identifier = parsed.path.rstrip("/").split("/")[-1]
        response = await client.get(f"https://data.rijksmuseum.nl/{identifier}", params={"_profile": "la-framed"})
        response.raise_for_status()
        return response.json()

    async def _resolve_work(self, client: httpx.AsyncClient, object_uri: str) -> Artwork | None:
        try:
            obj = await self._get_linked_record(client, object_uri)
            visual_ref = next((item.get("id") for item in (obj.get("shows") or []) if isinstance(item, dict) and item.get("id")), None)
            if not visual_ref:
                return None
            visual = await self._get_linked_record(client, visual_ref)
            digital_ref = next((item.get("id") for item in (visual.get("digitally_shown_by") or []) if isinstance(item, dict) and item.get("id")), None)
            if not digital_ref:
                return None
            digital = await self._get_linked_record(client, digital_ref)
            access_point = next((item.get("id") for item in (digital.get("access_point") or []) if isinstance(item, dict) and item.get("id")), None)
            if not access_point:
                return None
            # Normalise to an appropriately sized, direct IIIF image URL.
            parsed_image = urlparse(access_point)
            parts = parsed_image.path.strip("/").split("/")
            image_id = parts[0] if parts else ""
            image_url = f"https://iiif.micr.io/{image_id}/full/800,/0/default.jpg" if image_id else access_point
            title = next((item.get("content") for item in (obj.get("identified_by") or []) if isinstance(item, dict) and item.get("content")), None) or "Untitled"
            artist = "Unknown artist"
            production = obj.get("produced_by") or {}
            creators = production.get("carried_out_by") or [] if isinstance(production, dict) else []
            if creators and isinstance(creators[0], dict):
                artist = creators[0].get("_label") or creators[0].get("label") or artist
            year = None
            timespan = production.get("timespan") if isinstance(production, dict) else None
            if isinstance(timespan, dict):
                year = timespan.get("begin_of_the_begin") or timespan.get("_label")
            return Artwork(
                id=f"rijks:{urlparse(object_uri).path.rstrip('/').split('/')[-1]}",
                title=title,
                artist=artist,
                image_url=image_url,
                detail_url=object_uri,
                source=self.name,
                source_kind="institution",
                availability="not_for_sale",
                price=None,
                currency=None,
                medium=None,
                dimensions=None,
                year=str(year) if year else None,
                description=None,
            )
        except Exception as exc:
            logger.debug("Rijksmuseum image resolution failed (%s)", type(exc).__name__)
            return None


class ArtsyProvider:
    """Optional Artsy Partner API adapter.

    It is intentionally disabled unless both an XAPP token and partner ID are configured.
    The public API is not used as the application's foundation because Artsy is retiring it.
    """

    name = "Artsy"
    base_url = "https://api.artsy.net/api"

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]:
        if not ARTSY_XAPP_TOKEN or not ARTSY_PARTNER_ID:
            return []
        headers = {"X-XAPP-Token": ARTSY_XAPP_TOKEN, "Accept": "application/json"}
        # The partner API supports partner_id and published filters. Query filtering is
        # performed locally because the documented artworks endpoint does not expose q.
        params = {"partner_id": ARTSY_PARTNER_ID, "published": "true"}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers) as client:
            response = await client.get(f"{self.base_url}/artworks", params=params)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("_embedded", {}).get("artworks") or []
            return [
                work for work in (_map_artsy(item) for item in items)
                if _matches_query(work, query)
            ][:limit]


class Collect24Provider:
    """Optional commercial artwork registry adapter.

    Collect24 exposes public artwork records with explicit availability and
    public price fields. Access requires a server-side API key. The provider
    only returns works published as for-sale so the Buy experience never has
    to infer purchase status from museum data.
    """

    name = "Collect24"

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]:
        if not COLLECT24_API_KEY:
            return []
        headers = {
            "Authorization": f"Bearer {COLLECT24_API_KEY}",
            "Accept": "application/json",
        }
        params = {
            "q": query,
            "availability": "for_sale",
            "limit": min(limit, 100),
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers) as client:
            response = await client.get(f"{COLLECT24_BASE_URL}/artworks", params=params)
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data") or payload.get("results") or []
        return [self._map(row) for row in rows if isinstance(row, dict)][:limit]

    def _map(self, item: dict[str, Any]) -> Artwork:
        artist_data = item.get("artist") or {}
        price_data = item.get("price") or {}
        dimensions = item.get("dimensions_cm") or {}
        dimension_parts = []
        if isinstance(dimensions, dict):
            for key, label in (("height", "H"), ("width", "W"), ("depth", "D")):
                value = dimensions.get(key)
                if isinstance(value, (int, float)):
                    dimension_parts.append(f"{label} {value:g} cm")
        image_data = item.get("images") or {}
        image_url = _normalise_image_url(image_data.get("cover_url") or image_data.get("thumbnail_url")) if isinstance(image_data, dict) else None
        price_value = price_data.get("amount") if isinstance(price_data, dict) else None
        try:
            price_value = float(price_value) if price_value is not None else None
        except (TypeError, ValueError):
            price_value = None
        return Artwork(
            id=f"collect24:{item.get('collect24_id') or item.get('id')}",
            title=item.get("title") or "Untitled",
            artist=(artist_data.get("name") if isinstance(artist_data, dict) else None) or "Unknown artist",
            image_url=image_url,
            detail_url=(item.get("links") or {}).get("public_page") if isinstance(item.get("links"), dict) else None,
            source=self.name,
            source_kind="commercial",
            availability="available" if item.get("availability") == "for_sale" else "not_for_sale",
            price=price_value,
            currency=price_data.get("currency") if isinstance(price_data, dict) else None,
            medium=item.get("medium"),
            dimensions=" · ".join(dimension_parts) if dimension_parts else None,
            year=item.get("year_created"),
            description=item.get("provenance_summary"),
            purchase_mode="external",
            seller_name=(item.get("seller") or {}).get("name") if isinstance(item.get("seller"), dict) else None,
        )


def _map_artsy(item: dict[str, Any]) -> Artwork:
    artists = item.get("_embedded", {}).get("artists") or []
    artist = artists[0].get("name") if artists else "Unknown artist"
    can_acquire = bool(item.get("can_acquire"))
    can_inquire = bool(item.get("can_inquire"))
    availability = "available" if (can_acquire or can_inquire) and not item.get("sold") else "not_for_sale"
    price = None
    price_data = item.get("price") or item.get("list_price")
    if isinstance(price_data, (int, float)):
        price = float(price_data)
    elif isinstance(price_data, dict):
        amount = price_data.get("amount")
        if isinstance(amount, (int, float)):
            price = float(amount)
    if price is None and isinstance(item.get("price_cents"), (int, float)):
        price = float(item["price_cents"]) / 100.0
    return Artwork(
        id=f"artsy:{item.get('id')}",
        title=item.get("title") or "Untitled",
        artist=artist or "Unknown artist",
        image_url=_normalise_image_url((item.get("_links", {}).get("thumbnail", {}) or {}).get("href")),
        detail_url=(item.get("_links", {}).get("permalink", {}) or {}).get("href"),
        source="Artsy",
        source_kind="commercial",
        availability=availability,
        price=price,
        currency=item.get("currency") or (price_data.get("currency") if isinstance(price_data, dict) else None),
        medium=item.get("medium"),
        dimensions=(item.get("dimensions") or {}).get("cm", {}).get("text") if isinstance(item.get("dimensions"), dict) else item.get("dimensions"),
        year=str(item.get("date")) if item.get("date") else None,
        description=item.get("blurb"),
        purchase_mode="external",
        seller_name=None,
    )


class ArtlogicProvider:
    """Optional server-side Artlogic feed adapter.

    Artlogic feeds are configured per gallery/account and are not treated as a general public search API.
    Configure ARTLOGIC_FEED_URL only when the gallery has granted access.
    """

    name = "Artlogic"

    async def search(self, query: str, limit: int = ARTWORK_RESULT_LIMIT) -> list[Artwork]:
        if not ARTLOGIC_FEED_URL:
            return []
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(ARTLOGIC_FEED_URL)
            response.raise_for_status()
            payload = response.json()
        return [work for work in _map_artlogic(payload) if _matches_query(work, query)][:limit]


def _map_artlogic(payload: Any) -> list[Artwork]:
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    output: list[Artwork] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        available = str(row.get("status") or row.get("availability") or "").lower()
        sold = "sold" in available
        price = row.get("retail_price") or row.get("price")
        try:
            price_value = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price_value = None
        output.append(
            Artwork(
                id=f"artlogic:{row.get('id') or row.get('artwork_id')}",
                title=row.get("title") or "Untitled",
                artist=row.get("artist") or row.get("artist_name") or "Unknown artist",
                # Artlogic requires feed images to be imported and served locally;
                # do not hotlink the feed image URL. The source link remains available.
                image_url=None,
                detail_url=row.get("url") or row.get("permalink"),
                source="Artlogic",
                source_kind="commercial",
                availability="not_for_sale" if sold else "available",
                price=price_value,
                currency=row.get("retail_currency") or row.get("currency"),
                medium=row.get("medium") or row.get("artwork_type"),
                dimensions=row.get("dimensions"),
                year=str(row.get("year")) if row.get("year") else None,
                description=None,
                purchase_mode="external",
                seller_name=row.get("gallery") or row.get("gallery_name"),
            )
        )
    return output



def _normalise_image_url(value: Any) -> str | None:
    """Convert provider image templates into browser-ready HTTPS URLs."""
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    if url.startswith("//"):
        url = "https:" + url
    url = url.replace("{?width,height}", "?width=900&height=1125")
    url = url.replace("{?width}", "?width=900")
    url = url.replace("{?height}", "?height=1125")
    # Some APIs return templates with brace placeholders that need removal.
    url = url.replace("{width}", "900").replace("{height}", "1125")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _usable_image_url(value: Any) -> bool:
    return bool(_normalise_image_url(value))


def _matches_query(work: Artwork, query: str) -> bool:
    haystack = " ".join(part for part in [work.title, work.artist, work.medium, work.description] if part).lower()
    tokens = [token for token in query.lower().split() if len(token) > 3]
    return not tokens or any(token in haystack for token in tokens)


def commercial_providers() -> list[ArtworkProvider]:
    # Artsy is used only with current partner credentials; Artlogic is feed-based.
    return [ArtsyProvider(), ArtlogicProvider(), Collect24Provider()]


def institutional_providers() -> list[ArtworkProvider]:
    return [ArtInstituteProvider(), MetProvider(), ClevelandProvider(), RijksmuseumProvider()]

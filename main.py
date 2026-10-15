from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from html.parser import HTMLParser

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from models.schemas import ConciergeRequest
from services.concierge import run
from services.payments import PaymentUnavailable, PaymentConfigurationError, CheckoutSessionNotFound, create_checkout_session, retrieve_checkout_session, parse_webhook, verify_webhook_signature
from services.image_proxy import is_allowed_detail_url, is_allowed_image_url
from services.config import REQUEST_TIMEOUT

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
CONCIERGE_RESPONSE_TIMEOUT_SECONDS = 4.6

app = FastAPI(
    title="ArtCo | Your personal art concierge",
    description="ArtCo is a personal art concierge for cultural discovery, artwork research, curation and acquisition.",
    version="0.4.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html", context={"page": "home"})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"page": "dashboard"})


@app.get("/concierge", response_class=HTMLResponse)
async def concierge_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="concierge.html", context={"page": "concierge"})


@app.get("/results", response_class=HTMLResponse)
async def results_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="results.html", context={"page": "results"})


@app.get("/saved", response_class=HTMLResponse)
async def saved_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="saved.html", context={"page": "saved"})


@app.get("/taste")
async def taste_page(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/dashboard#taste", status_code=307)


@app.get("/checkout/success", response_class=HTMLResponse)
async def checkout_success(request: Request) -> HTMLResponse:
    # A redirect to this URL proves only that the browser returned from Stripe.
    # The page must verify the Checkout Session before telling the buyer payment succeeded.
    return templates.TemplateResponse(request=request, name="checkout_status.html", context={
        "page": "checkout", "status": "verify",
        "session_id": request.query_params.get("session_id", ""),
    })


@app.get("/checkout/cancel", response_class=HTMLResponse)
async def checkout_cancel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="checkout_status.html", context={"page": "checkout", "status": "cancel"})


@app.post("/api/checkout/session")
async def create_checkout(payload: dict[str, Any]) -> JSONResponse:
    artwork_id = str(payload.get("artwork_id") or "").strip()
    if not artwork_id:
        return JSONResponse({"success": False, "detail": "Artwork ID is required."}, status_code=400)
    try:
        session = await create_checkout_session(artwork_id)
    except PaymentUnavailable as exc:
        return JSONResponse({"success": False, "detail": str(exc)}, status_code=409)
    except PaymentConfigurationError as exc:
        return JSONResponse({"success": False, "detail": str(exc)}, status_code=422)
    except Exception:
        return JSONResponse({"success": False, "detail": "Stripe Checkout could not be started."}, status_code=502)
    return JSONResponse({"success": True, "checkout_url": session.get("url"), "session_id": session.get("id")})


@app.get("/api/checkout/status")
async def checkout_status(session_id: str = "") -> JSONResponse:
    """Verify a Checkout Session with Stripe and return a minimal buyer-safe status."""
    try:
        session = await retrieve_checkout_session(session_id)
    except CheckoutSessionNotFound as exc:
        return JSONResponse({"success": False, "detail": str(exc)}, status_code=404)
    except PaymentUnavailable as exc:
        return JSONResponse({"success": False, "detail": str(exc)}, status_code=409)
    except Exception:
        logger.exception("Could not verify Stripe Checkout Session")
        return JSONResponse({"success": False, "detail": "Stripe could not verify this checkout right now. Try again shortly."}, status_code=502)
    return JSONResponse({"success": True, **session})


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    if not verify_webhook_signature(payload, signature):
        return JSONResponse({"success": False, "detail": "Invalid Stripe webhook signature."}, status_code=400)
    event = parse_webhook(payload)
    event_type = event.get("type", "")
    # Keep webhook handling intentionally small until durable order storage is introduced.
    if event_type == "checkout.session.completed":
        session = (event.get("data") or {}).get("object") or {}
        request.app.state.last_completed_checkout = {
            "session_id": session.get("id"),
            "artwork_id": (session.get("metadata") or {}).get("artwork_id"),
            "completed_at": session.get("created"),
        }
    return JSONResponse({"success": True, "received": True})


MAX_PROXY_IMAGE_BYTES = 8 * 1024 * 1024
_PROXY_REDIRECT_CODES = {301, 302, 303, 307, 308}

def _sniff_image_media_type(declared_type: str, payload: bytes) -> str | None:
    """Accept valid image bytes from providers that label them octet-stream."""
    media_type = (declared_type or "").split(";", 1)[0].strip().lower()
    if media_type.startswith("image/"):
        return media_type
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if len(payload) >= 12 and payload[4:8] == b"ftyp" and payload[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return None





class _ArtworkImageMetadataParser(HTMLParser):
    """Extract likely artwork images from metadata and rendered HTML markup."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []
        self.inline_candidates: list[tuple[int, str]] = []

    @staticmethod
    def _srcset_urls(value: str) -> list[str]:
        urls: list[str] = []
        for entry in value.split(","):
            parts = entry.strip().split()
            if parts:
                urls.append(parts[0])
        return urls

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value for key, value in attrs if key and value}
        tag_name = tag.lower()
        if tag_name == "meta":
            key = (attributes.get("property") or attributes.get("name") or attributes.get("itemprop") or "").lower()
            if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src", "image"}:
                content = attributes.get("content")
                if content:
                    self.candidates.append(content.strip())
        elif tag_name == "link":
            rel = (attributes.get("rel") or "").lower().split()
            if "image_src" in rel or ("image" in rel and attributes.get("itemprop") == "image"):
                href = attributes.get("href")
                if href:
                    self.candidates.append(href.strip())
        elif tag_name == "img":
            image_values = []
            for key in ("data-full-image", "data-zoom-image", "data-original", "data-lazy-src", "data-src", "src"):
                value = attributes.get(key)
                if value:
                    image_values.append(value.strip())
            image_values.extend(self._srcset_urls(attributes.get("srcset", "")))
            context = " ".join(attributes.get(key, "") for key in ("alt", "title", "class", "id")).lower()
            score = 20
            if any(word in context for word in ("artwork", "object-image", "primary-image", "collection-image", "art-image", "painting", "sculpture")):
                score += 40
            if any(word in context for word in ("logo", "avatar", "icon", "profile", "social")):
                score -= 80
            width = attributes.get("width", "")
            height = attributes.get("height", "")
            try:
                if width.isdigit() and height.isdigit():
                    score += 20 if int(width) >= 250 and int(height) >= 250 else -20 if int(width) < 80 or int(height) < 80 else 0
            except (TypeError, ValueError):
                pass
            for value in image_values:
                self.inline_candidates.append((score, value))


async def _get_allowed_redirected_url(
    client: httpx.AsyncClient,
    url: str,
    allow_url: Any,
    headers: dict[str, str],
) -> httpx.Response:
    current_url = url
    for _ in range(4):
        if not allow_url(current_url):
            raise HTTPException(status_code=400, detail="This artwork source is not supported by ArtCo.")
        response = await client.get(current_url, headers=headers, follow_redirects=False)
        if response.status_code in _PROXY_REDIRECT_CODES:
            location = response.headers.get("location")
            if not location:
                raise HTTPException(status_code=502, detail="The art source returned an invalid redirect.")
            current_url = urljoin(current_url, location)
            continue
        return response
    raise HTTPException(status_code=502, detail="The art source redirected too many times.")


@app.get("/api/artwork-preview")
async def artwork_preview_from_source(url: str) -> Response:
    """Resolve an artwork page's primary image when its API thumbnail is missing/broken."""
    if not is_allowed_detail_url(url):
        raise HTTPException(status_code=400, detail="This artwork source page is not supported by ArtCo.")
    headers = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "User-Agent": "Mozilla/5.0 (compatible; ArtCoArtworkPreview/1.0)",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            page = await _get_allowed_redirected_url(client, url, is_allowed_detail_url, headers)
            if page.status_code != 200:
                raise HTTPException(status_code=502, detail="The artwork source page could not be read.")
            page_type = page.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if page_type not in {"text/html", "application/xhtml+xml"}:
                raise HTTPException(status_code=502, detail="The artwork source did not return a collection page.")
            if len(page.content) > 2 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="The artwork source page is too large to inspect.")
            parser = _ArtworkImageMetadataParser()
            parser.feed(page.text)
            page_base = str(page.url or url)
            raw_candidates = list(parser.candidates) + [candidate for _, candidate in sorted(parser.inline_candidates, key=lambda item: item[0], reverse=True)]
            image_url = None
            for candidate in raw_candidates:
                candidate_url = urljoin(page_base, candidate.strip())
                # Many collection pages still publish http image metadata. Upgrade
                # only when the exact same hostname is already in the HTTPS allowlist.
                if candidate_url.lower().startswith("http://"):
                    secure_candidate = "https://" + candidate_url[len("http://"):]
                    if is_allowed_image_url(secure_candidate):
                        candidate_url = secure_candidate
                if is_allowed_image_url(candidate_url):
                    image_url = candidate_url
                    break
            if not image_url:
                raise HTTPException(status_code=404, detail="No supported artwork preview was found on the source page.")
            image_response = await _get_allowed_redirected_url(
                client,
                image_url,
                is_allowed_image_url,
                {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8", "User-Agent": "ArtCoArtworkImageProxy/1.0"},
            )
            if image_response.status_code != 200:
                raise HTTPException(status_code=502, detail="The artwork preview could not be loaded.")
            if len(image_response.content) > MAX_PROXY_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="The artwork preview is too large to display.")
            content_type = _sniff_image_media_type(
                image_response.headers.get("content-type", ""), image_response.content
            )
            if not content_type:
                raise HTTPException(status_code=502, detail="The source preview was not an image.")
            return Response(
                content=image_response.content,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"},
            )
    except HTTPException:
        raise
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.info("Artwork preview fallback failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="The artwork preview is temporarily unavailable.") from exc


@app.get("/api/artwork-image")
async def proxy_artwork_image(url: str) -> Response:
    """Serve approved public art thumbnails same-origin when browser hotlinking fails.

    Every redirect is revalidated against an allowlist. Arbitrary hosts, non-HTTPS
    URLs, non-image responses, and oversized responses are rejected.
    """
    if not is_allowed_image_url(url):
        raise HTTPException(status_code=400, detail="This image source is not supported by the ArtCo image proxy.")

    current_url = url
    headers = {
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "User-Agent": "ArtCoArtworkImageProxy/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            for _ in range(4):
                async with client.stream("GET", current_url, headers=headers) as upstream:
                    if upstream.status_code in _PROXY_REDIRECT_CODES:
                        location = upstream.headers.get("location")
                        if not location:
                            raise HTTPException(status_code=502, detail="The image provider returned an invalid redirect.")
                        next_url = urljoin(current_url, location)
                        if not is_allowed_image_url(next_url):
                            raise HTTPException(status_code=502, detail="The image provider redirected to an unsupported host.")
                        current_url = next_url
                        continue
                    if upstream.status_code != 200:
                        raise HTTPException(status_code=502, detail="The image provider could not return this artwork image.")
                    declared_content_type = upstream.headers.get("content-type", "")
                    declared_size = upstream.headers.get("content-length")
                    if declared_size and declared_size.isdigit() and int(declared_size) > MAX_PROXY_IMAGE_BYTES:
                        raise HTTPException(status_code=413, detail="The artwork image is too large to display.")
                    chunks: list[bytes] = []
                    total_size = 0
                    async for chunk in upstream.aiter_bytes():
                        total_size += len(chunk)
                        if total_size > MAX_PROXY_IMAGE_BYTES:
                            raise HTTPException(status_code=413, detail="The artwork image is too large to display.")
                        chunks.append(chunk)
                    image_payload = b"".join(chunks)
                    content_type = _sniff_image_media_type(declared_content_type, image_payload)
                    if not content_type:
                        raise HTTPException(status_code=502, detail="The image provider returned a non-image response.")
                    return Response(
                        content=image_payload,
                        media_type=content_type,
                        headers={
                            "Cache-Control": "public, max-age=86400",
                            "X-Content-Type-Options": "nosniff",
                            "Content-Disposition": "inline",
                        },
                    )
    except HTTPException:
        raise
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.info("Artwork image proxy failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="The artwork image is temporarily unavailable.") from exc
    raise HTTPException(status_code=502, detail="The image provider redirected too many times.")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/api/diagnostics")
async def diagnostics() -> dict[str, Any]:
    from services import config
    return {
        "qloo": {
            "configured": bool(config.QLOO_API_KEY),
            "base_url": config.QLOO_BASE_URL,
            "hackathon_environment": config.QLOO_BASE_URL == "https://hackathon.api.qloo.com",
        },
        "gemini": {
            "configured": bool(config.GEMINI_API_KEY),
            "model": config.GEMINI_MODEL,
        },
        "commercial_sources": {
            "artsy": bool(config.ARTSY_XAPP_TOKEN and config.ARTSY_PARTNER_ID),
            "artlogic": bool(config.ARTLOGIC_FEED_URL),
            "collect24": bool(config.COLLECT24_API_KEY),
        },
        "stripe": {
            "configured": bool(config.STRIPE_SECRET_KEY),
        },
    }


@app.get("/api/diagnostics/live")
async def diagnostics_live() -> dict[str, Any]:
    """Safe live smoke test for the configured model APIs and open art sources.

    This endpoint returns configuration/health metadata and counts only; it never
    returns credentials or raw upstream response bodies.
    """
    from services import config
    from services.qloo import search_entities, get_artist_insights
    from services.llm import extract_request_signals
    from services.art_sources import ArtInstituteProvider, MetProvider, ClevelandProvider

    async def probe(label: str, operation, count_field: str | None = None) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(operation(), timeout=18)
            count = len(result) if hasattr(result, "__len__") else None
            value = {"name": label, "ok": True}
            if count_field:
                value[count_field] = count
            return value
        except Exception as exc:
            logger.warning("Live diagnostics probe %s failed (%s)", label, type(exc).__name__)
            return {"name": label, "ok": False, "error": type(exc).__name__}

    async def qloo_probe() -> dict[str, Any]:
        try:
            entities = await asyncio.wait_for(search_entities("Radiohead"), timeout=18)
            entity_ids = [str(item.get("id")) for item in entities if item.get("id")][:5]
            insights = await asyncio.wait_for(get_artist_insights(entity_ids, 50), timeout=18) if entity_ids else []
            return {
                "name": "Qloo Search + Insights",
                "ok": bool(entities) and (bool(insights) or not entity_ids),
                "entities_found": len(entities),
                "insights_found": len(insights),
            }
        except Exception as exc:
            logger.warning("Live Qloo probe failed (%s)", type(exc).__name__)
            return {"name": "Qloo Search + Insights", "ok": False, "error": type(exc).__name__}

    gemini_task = probe(
        "Gemini request interpretation",
        lambda: extract_request_signals("Find contemporary art inspired by Radiohead", [], []),
        None,
    )
    aic_task = probe("Art Institute of Chicago", lambda: ArtInstituteProvider().search("contemporary art", limit=2), "works_found")
    met_task = probe("The Met", lambda: MetProvider().search("contemporary art", limit=2), "works_found")
    cleveland_task = probe("Cleveland Museum of Art", lambda: ClevelandProvider().search("contemporary art", limit=2), "works_found")

    qloo_result, gemini_result, aic_result, met_result, cleveland_result = await asyncio.gather(
        qloo_probe(), gemini_task, aic_task, met_task, cleveland_task
    )
    return {
        "qloo_base_url": config.QLOO_BASE_URL,
        "qloo_configured": bool(config.QLOO_API_KEY),
        "gemini_configured": bool(config.GEMINI_API_KEY),
        "probes": [qloo_result, gemini_result, aic_result, met_result, cleveland_result],
        "hint": "If a probe is not configured or fails, review the matching environment variable and deployment logs.",
    }


@app.post("/api/concierge")
async def concierge(payload: ConciergeRequest) -> JSONResponse:
    try:
        result: dict[str, Any] = await asyncio.wait_for(
            run(payload), timeout=CONCIERGE_RESPONSE_TIMEOUT_SECONDS
        )
        # Enforce the visible-card limit at the API boundary as well as in the
        # agent and UI. This prevents a stale/alternate agent response from
        # rendering a second shortlist beneath the primary three cards.
        primary = list(result.get("results") or [])[:3]
        remaining = max(0, 3 - len(primary))
        secondary = list(result.get("not_for_sale") or [])[:remaining] if payload.intent.value == "buy" else []
        result["results"] = primary
        result["not_for_sale"] = secondary
        return JSONResponse(result)
    except asyncio.TimeoutError:
        logger.warning("Concierge request exceeded the %.1f second API deadline", CONCIERGE_RESPONSE_TIMEOUT_SECONDS)
        return JSONResponse(
            {
                "success": False,
                "detail": "This search exceeded ArtCo’s response-time limit. Try a more focused request.",
                "error_type": "ResponseDeadlineExceeded",
                "diagnostics": {"response_target_ms": int(CONCIERGE_RESPONSE_TIMEOUT_SECONDS * 1000)},
            },
            status_code=504,
        )
    except Exception as exc:
        logger.exception("Concierge request failed")
        # Preserve a useful, non-sensitive API response instead of returning an
        # unexplained framework 500 page to the frontend.
        return JSONResponse(
            {
                "success": False,
                "detail": "The concierge could not complete this search. Check the service configuration and try again.",
                "error_type": type(exc).__name__,
            },
            status_code=502,
        )

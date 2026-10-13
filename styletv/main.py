from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .catalog import PRODUCTS, SHOWS, get_product
from .tvmaze import get_show_catalog

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("styletv")

SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
if os.getenv("VERCEL", "0") == "1" and not SESSION_SECRET:
    raise RuntimeError("Set SESSION_SECRET in Vercel Environment Variables before deploying styleTV.")

app = FastAPI(title="styleTV", version="0.1.0", description="TV-inspired personal fashion discovery and virtual try-on.")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or "local-only-change-me-please-replace",
    session_cookie="styletv_demo_session",
    max_age=60 * 60 * 8,
    same_site="lax",
    https_only=os.getenv("VERCEL", "0") == "1",
)
# Vercel serves public/** assets through its CDN; mount them directly only for local Uvicorn runs.
if os.getenv("VERCEL", "0") != "1":
    app.mount("/assets", StaticFiles(directory=str(PUBLIC_DIR / "assets")), name="assets")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

YCE_BASE = os.getenv("YCE_API_BASE", "https://yce-api-01.makeupar.com").rstrip("/")
YCE_API_KEY = os.getenv("YCE_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
MAX_UPLOAD_BYTES = 9_500_000
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png"}

# These provider paths match the current YouCam API reference documentation.
YCE_TASK_PATHS = {
    "try-on": "/s2s/v2.0/task/cloth-v4",
    "color": "/s2s/v2.0/task/skin-tone-analysis",
}


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_session(request: Request) -> dict:
    user = request.session.get("demo_user")
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        provided = request.headers.get("x-csrf-token", "")
        if not provided or not secrets.compare_digest(provided, str(request.session.get("csrf_token", ""))):
            raise HTTPException(status_code=403, detail="Your session token expired. Refresh the page and try again.")
    return user


def normalize_yce_content_type(content_type: str) -> str:
    """Map browser image/jpeg to the image/jpg MIME type used in YouCam File API examples."""
    return "image/png" if content_type == "image/png" else "image/jpg"


def yce_headers() -> dict[str, str]:
    if not YCE_API_KEY:
        raise HTTPException(status_code=503, detail="YouCam is not configured. Add YCE_API_KEY to the environment to enable live analysis and try-on.")
    return {"Authorization": f"Bearer {YCE_API_KEY}", "Content-Type": "application/json"}


async def upload_image_to_yce(image_bytes: bytes, filename: str, content_type: str) -> str:
    """Upload bytes using YouCam's metadata + presigned URL workflow."""
    if not image_bytes or len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Choose an image smaller than 9.5 MB.")
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Use a JPG or PNG image.")

    # YouCam's File API examples use image/jpg for JPEG uploads. Browsers normally
    # report image/jpeg, so normalize the provider-facing metadata while preserving
    # the original accepted input types at the HTTP boundary.
    provider_content_type = normalize_yce_content_type(content_type)
    clean_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename or "upload.jpg").name)[:100]
    if not clean_name.lower().endswith((".jpg", ".jpeg", ".png")):
        clean_name += ".png" if provider_content_type == "image/png" else ".jpg"

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        metadata_response = await client.post(
            f"{YCE_BASE}/s2s/v2.0/file",
            headers=yce_headers(),
            json={"files": [{"content_type": provider_content_type, "file_name": clean_name, "file_size": len(image_bytes)}]},
        )
        if metadata_response.is_error:
            logger.warning("YouCam file metadata request failed: %s", metadata_response.status_code)
            raise HTTPException(status_code=502, detail="YouCam could not prepare the image upload. Check your API access and try again.")
        try:
            file_payload = metadata_response.json()["data"]["files"][0]
            file_id = file_payload["file_id"]
            upload_request = file_payload["requests"][0]
            upload_url = upload_request["url"]
            upload_headers = upload_request.get("headers", {})
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.exception("Unexpected YouCam file response")
            raise HTTPException(status_code=502, detail="YouCam returned an unexpected image-upload response.") from exc

        upload_response = await client.put(upload_url, headers=upload_headers, content=image_bytes)
        if upload_response.is_error:
            logger.warning("YouCam signed upload failed: %s", upload_response.status_code)
            raise HTTPException(status_code=502, detail="The image could not be transferred to YouCam. Please retry with a different image.")
    return str(file_id)


def extract_task_status(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw = data.get("task_status") or data.get("status") or payload.get("task_status") or payload.get("status")
    if isinstance(raw, str):
        normalized = raw.lower()
        if normalized in {"success", "succeeded", "completed", "complete", "done"}:
            return "success"
        if normalized in {"error", "failed", "failure", "cancelled", "canceled"}:
            return "error"
        if normalized in {"running", "pending", "queued", "processing", "created"}:
            return "running"
    if data.get("results") and not data.get("task_status"):
        return "success"
    return "running"


def extract_output_url(value: Any) -> str | None:
    """Best-effort extraction of the generated image URL from YouCam result payloads."""
    preferred_keys = {"result_url", "output_url", "image_url", "generated_image_url", "dst_url", "download_url", "url"}
    if isinstance(value, dict):
        for key in preferred_keys:
            item = value.get(key)
            if isinstance(item, str) and item.startswith("https://"):
                return item
        for key, item in value.items():
            if key in {"mask_urls", "requests", "upload_url"}:
                continue
            found = extract_output_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = extract_output_url(item)
            if found:
                return found
    return None


def find_color_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    results = data.get("results") if isinstance(data.get("results"), dict) else {}
    colors = results.get("color") or data.get("color")
    if isinstance(colors, dict):
        return colors
    return None


async def create_yce_task(path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{YCE_BASE}{path}", headers=yce_headers(), json=body)
        if response.is_error:
            logger.warning("YouCam task creation failed: %s %s", path, response.status_code)
            detail = "YouCam could not start this task. Check API access, remaining units, and the image requirements."
            if response.status_code == 401:
                detail = "YouCam rejected the API key. Check YCE_API_KEY."
            elif response.status_code == 429:
                detail = "YouCam rate limit or unit limit reached. Try again later or check your account."
            raise HTTPException(status_code=502, detail=detail)
        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="YouCam returned an unexpected task response.") from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "styleTV"}


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"request": request, "signed_in": bool(request.session.get("demo_user")), "csrf_token": csrf_token(request)},
    )


@app.get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request, error: str | None = None):
    if request.session.get("demo_user"):
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(
        request,
        "signin.html",
        {"request": request, "error": error, "csrf_token": csrf_token(request)},
    )


@app.post("/signin")
async def signin(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form(...)):
    expected_csrf = str(request.session.get("csrf_token", ""))
    if not expected_csrf or not secrets.compare_digest(csrf, expected_csrf):
        return RedirectResponse("/signin?error=expired", status_code=303)
    demo_email = os.getenv("DEMO_EMAIL", "demo@styletv.app").strip().lower()
    demo_password = os.getenv("DEMO_PASSWORD", "styleTV2026")
    if not secrets.compare_digest(email.strip().lower(), demo_email) or not secrets.compare_digest(password, demo_password):
        return RedirectResponse("/signin?error=invalid", status_code=303)
    request.session.clear()
    request.session["demo_user"] = {"email": demo_email, "name": "Demo stylist"}
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse("/app", status_code=303)


@app.get("/signout", response_class=HTMLResponse)
async def signout_page(request: Request, done: bool = False):
    signed_in = bool(request.session.get("demo_user"))
    return templates.TemplateResponse(
        request,
        "signout.html",
        {"request": request, "signed_in": signed_in, "done": done, "csrf_token": csrf_token(request)},
    )


@app.post("/signout")
async def signout(request: Request, csrf: str = Form(...)):
    expected_csrf = str(request.session.get("csrf_token", ""))
    if expected_csrf and secrets.compare_digest(csrf, expected_csrf):
        request.session.clear()
    return RedirectResponse("/signout?done=1", status_code=303)


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    if not request.session.get("demo_user"):
        return RedirectResponse("/signin?next=/app", status_code=303)
    return templates.TemplateResponse(
        request,
        "app.html",
        {"request": request, "user": request.session["demo_user"], "csrf_token": csrf_token(request)},
    )


@app.get("/api/config")
async def config(request: Request):
    require_session(request)
    return {
        "gemini_configured": bool(GEMINI_API_KEY),
        "gemini_model": GEMINI_MODEL,
        "youcam_configured": bool(YCE_API_KEY),
        "storage": "IndexedDB (this browser)",
        "demo_mode": True,
    }


@app.get("/api/catalog")
async def api_catalog(request: Request):
    require_session(request)
    catalogue = await get_show_catalog(SHOWS)
    return {
        "shows": catalogue["shows"],
        "products": PRODUCTS,
        "catalogue": catalogue["catalogue"],
    }


class RecommendationRequest(BaseModel):
    query: str = Field(default="", max_length=1200)
    profile: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=6, ge=1, le=12)


def deterministic_rank(profile: dict[str, Any], query: str = "", shows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    selected_shows = set(profile.get("favoriteShows") or [])
    show_catalog = shows or SHOWS
    selected_show_tags: set[str] = set()
    query_lower = query.lower()
    # A user choosing "Explore looks" from a show card should get items linked to that show,
    # even when it is not already one of their favourites.
    for show in show_catalog:
        if show["name"].lower() in query_lower:
            selected_shows.add(show["id"])
        if show.get("id") in selected_shows:
            selected_show_tags.update(str(tag).lower() for tag in (show.get("tags") or []))
    selected_tags = {str(item).lower() for item in (profile.get("styleTags") or [])}
    selected_patterns = {str(item).lower() for item in (profile.get("patterns") or [])}
    preferred_colors = {str(item).lower() for item in (profile.get("colorPreferences") or [])}
    budget = profile.get("budget")
    try:
        budget = float(budget) if budget not in (None, "", "any") else None
    except (TypeError, ValueError):
        budget = None
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    # An explicit budget in the current request takes precedence over the saved budget.
    request_budget = re.search(r"\b(?:under|below|less than|up to|max(?:imum)?)\s*\$?\s*(\d+(?:\.\d+)?)", query_lower)
    if request_budget:
        budget = float(request_budget.group(1))
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in PRODUCTS:
        if budget is not None and float(item["price"]) > budget:
            continue
        score = 0
        item_tags = {x.lower() for x in item.get("tags", [])}
        item_patterns = {x.lower() for x in item.get("patterns", [])}
        item_colors = {x.lower() for x in item.get("colors", [])}
        item_shows = set(item.get("shows", []))
        score += 3 * len(selected_tags & item_tags)
        # Show tags come from curated starter cues or transparent metadata-based
        # inferences. They influence garment recommendations without claiming
        # that TVmaze contains episode-specific costume records.
        score += 2 * len(selected_show_tags & item_tags)
        score += 1 * len(selected_show_tags & item_patterns)
        score += 2 * len(selected_patterns & item_patterns)
        score += 1 * len(preferred_colors & item_colors)
        score += 3 if selected_shows & item_shows else 0
        if budget is not None:
            score += 4 if float(item["price"]) <= budget else -2
        searchable = " ".join([item["name"], item["category"], *item["tags"], *item["patterns"], *item["colors"]]).lower()
        score += min(8, sum(1 for word in words if word in searchable))
        scored.append((score, item))
    scored.sort(key=lambda entry: (-entry[0], float(entry[1]["price"])))
    return [item for _, item in scored]


def rank_show_recommendations(profile: dict[str, Any], shows: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    """Rank new shows using user aesthetic tags plus metadata genres from TVmaze."""
    favorites = set(profile.get("favoriteShows") or [])
    by_id = {show.get("id"): show for show in shows}
    favorite_genres: set[str] = set()
    for show_id in favorites:
        favorite_genres.update(str(g).casefold() for g in (by_id.get(show_id, {}).get("genres") or []))
    profile_tags = {str(tag).casefold() for tag in (profile.get("styleTags") or [])}
    profile_patterns = {str(tag).casefold() for tag in (profile.get("patterns") or [])}

    def score(show: dict[str, Any]) -> tuple[float, float]:
        tags = {str(tag).casefold() for tag in (show.get("tags") or [])}
        genres = {str(genre).casefold() for genre in (show.get("genres") or [])}
        value = 3.0 * len(tags & profile_tags) + len(tags & profile_patterns)
        value += 1.6 * len(genres & favorite_genres)
        rating = show.get("rating")
        return value, float(rating or 0)

    candidates = [show for show in shows if show.get("id") not in favorites]
    candidates.sort(key=score, reverse=True)
    return candidates[:limit]


async def gemini_recommendations(
    profile: dict[str, Any], query: str, candidates: list[dict[str, Any]], shows: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai

        candidate_data = [
            {"id": x["id"], "name": x["name"], "tags": x["tags"], "patterns": x["patterns"], "shows": x["shows"], "price_sample_usd": x["price"]}
            for x in candidates
        ]
        show_catalog = shows or SHOWS
        show_shortlist = rank_show_recommendations(profile, show_catalog, limit=40)
        known_shows = [
            {"id": item["id"], "name": item["name"], "genres": item.get("genres", []), "style_cues": item.get("tags", []), "synopsis": item.get("description", "")[:180]}
            for item in show_shortlist
        ]
        prompt = (
            "You power styleTV, a TV-inspired fashion discovery app. Interpret the request and rank only existing catalogue items. "
            "Never invent shows, garments, prices, retailers, or availability. Return valid JSON only with keys: product_ids (array of known product ids), "
            "show_ids (array of known show ids selected only from the supplied show shortlist), explanation (one short sentence), "
            "interpreted_preferences (object with tags and patterns arrays). Show genre/synopsis similarity is a discovery hint, not proof of wardrobe similarity. "
            "Do not infer or recommend clothing based on race or skin type. Facial colour-tone results are optional styling inspiration only.\n"
            f"USER QUERY: {query}\nPROFILE: {json.dumps(profile, ensure_ascii=False)}\nCATALOG: {json.dumps(candidate_data, ensure_ascii=False)}\n"
            f"SHOW SHORTLIST: {json.dumps(known_shows, ensure_ascii=False)}"
        )
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        text = getattr(response, "text", None)
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        parsed = json.loads(cleaned)
        valid_product_ids = {x["id"] for x in candidates}
        valid_show_ids = {x["id"] for x in known_shows}
        parsed["product_ids"] = [x for x in parsed.get("product_ids", []) if x in valid_product_ids]
        parsed["show_ids"] = [x for x in parsed.get("show_ids", []) if x in valid_show_ids]
        return parsed
    except Exception:
        logger.exception("Gemini recommendation failed; falling back to deterministic ranking")
        return None


@app.post("/api/recommendations")
async def api_recommendations(body: RecommendationRequest, request: Request):
    require_session(request)
    catalogue = await get_show_catalog(SHOWS)
    shows = catalogue["shows"]
    ranked = deterministic_rank(body.profile, body.query, shows)
    ai = await gemini_recommendations(body.profile, body.query, ranked, shows) if GEMINI_API_KEY else None
    valid_ids = {x["id"] for x in ranked}
    if ai:
        ai_ids = [item_id for item_id in ai.get("product_ids", []) if item_id in valid_ids]
        by_id = {x["id"]: x for x in ranked}
        ordered = [by_id[x] for x in ai_ids]
        ordered.extend(x for x in ranked if x["id"] not in set(ai_ids))
        ranked = ordered
    favorite_ids = set(body.profile.get("favoriteShows") or [])
    related_shows = rank_show_recommendations(body.profile, shows, limit=12)
    ai_show_ids = [show_id for show_id in (ai or {}).get("show_ids", []) if show_id not in favorite_ids]
    related_by_id = {show["id"]: show for show in related_shows}
    show_recommendations = [related_by_id[show_id] for show_id in ai_show_ids if show_id in related_by_id]
    if not show_recommendations:
        show_recommendations = related_shows[:6]
    return {
        "products": ranked[: body.limit],
        "explanation": (ai or {}).get("explanation") or "Matched against your saved shows, style preferences, patterns and budget.",
        "source": "gemini" if ai else "catalogue-matcher",
        "model": GEMINI_MODEL if ai else None,
        "show_recommendations": [show["id"] for show in show_recommendations],
        "show_recommendation_details": show_recommendations,
        "catalogue_source": catalogue["catalogue"],
        "interpreted_preferences": (ai or {}).get("interpreted_preferences", {}),
    }


@app.post("/api/try-on/{product_id}")
async def try_on(
    product_id: str,
    request: Request,
    photo: UploadFile = File(...),
    consent: bool = Form(...),
):
    require_session(request)
    if not consent:
        raise HTTPException(status_code=400, detail="Consent is required before sending your photo to YouCam.")
    product = get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="This style was not found in the catalogue.")
    if product.get("try_on_eligible", True) is False:
        raise HTTPException(status_code=400, detail="This reference image is not eligible for YouCam Clothes V4 yet. Choose another garment for try-on.")
    if not product.get("try_on_reference_url"):
        raise HTTPException(status_code=400, detail="This style does not have a try-on reference image yet.")
    content_type = (photo.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Use a JPG or PNG photo.")
    photo_bytes = await photo.read(MAX_UPLOAD_BYTES + 1)
    file_id = await upload_image_to_yce(photo_bytes, photo.filename or "styleTV-photo.jpg", content_type)
    payload = await create_yce_task(
        YCE_TASK_PATHS["try-on"],
        {
            "src_file_id": file_id,
            "ref_file_url": product["try_on_reference_url"],
            "garment_category": product.get("garment_category", "auto"),
            "filter_multi_person": "normal",
        },
    )
    try:
        task_id = payload["data"]["task_id"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="YouCam did not return a try-on task ID.") from exc
    return {"task_id": task_id, "task_kind": "try-on", "product_id": product_id, "message": "Your try-on is processing."}


@app.post("/api/color-analysis")
async def color_analysis(request: Request, photo: UploadFile = File(...), consent: bool = Form(...)):
    require_session(request)
    if not consent:
        raise HTTPException(status_code=400, detail="Consent is required before sending your photo to YouCam.")
    content_type = (photo.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Use a JPG or PNG selfie.")
    photo_bytes = await photo.read(MAX_UPLOAD_BYTES + 1)
    file_id = await upload_image_to_yce(photo_bytes, photo.filename or "styleTV-colour-selfie.jpg", content_type)
    payload = await create_yce_task(
        YCE_TASK_PATHS["color"],
        {"src_file_id": file_id, "face_angle_strictness_level": "high"},
    )
    try:
        task_id = payload["data"]["task_id"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="YouCam did not return a colour-analysis task ID.") from exc
    return {"task_id": task_id, "task_kind": "color", "message": "Your colour analysis is processing."}


@app.get("/api/tasks/{kind}/{task_id:path}")
async def task_status(kind: str, task_id: str, request: Request):
    require_session(request)
    if kind not in {"try-on", "color"}:
        raise HTTPException(status_code=404, detail="Unknown task type.")
    if not task_id or len(task_id) > 512 or ".." in task_id:
        raise HTTPException(status_code=400, detail="Invalid task ID.")
    endpoint = YCE_TASK_PATHS[kind] + "/"
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.get(f"{YCE_BASE}{endpoint}{task_id}", headers=yce_headers())
        if response.is_error:
            logger.warning("YouCam task status lookup failed: %s", response.status_code)
            raise HTTPException(status_code=502, detail="Could not retrieve the YouCam task status. Retry in a moment.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="YouCam returned an unexpected task status response.") from exc

    status = extract_task_status(payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result: dict[str, Any] = {"status": status, "task_kind": kind}
    if status == "error":
        result["message"] = data.get("error_message") or payload.get("error_message") or "The task could not be completed. Try a different photo."
    elif status == "success":
        if kind == "color":
            result["colors"] = find_color_payload(payload) or {}
            result["message"] = "Colour analysis completed. Use these colour cues as inspiration, not rules."
        else:
            result["result_url"] = extract_output_url(payload)
            if not result["result_url"]:
                result["message"] = "YouCam completed the task, but the response did not include a recognized result image URL."
            else:
                result["message"] = "Your try-on is ready. This is a visual preview, not a fit guarantee."
    return result

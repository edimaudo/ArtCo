from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from models.schemas import ConciergeRequest
from services.concierge import run
from services.payments import PaymentUnavailable, PaymentConfigurationError, create_checkout_session, parse_webhook, verify_webhook_signature

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="ArtCo | Your personal art concierge",
    description="ArtCo is a personal art concierge for cultural discovery, artwork research, curation and acquisition.",
    version="0.3.0",
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
    return templates.TemplateResponse(request=request, name="checkout_status.html", context={"page": "checkout", "status": "success"})


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


@app.post("/api/concierge")
async def concierge(payload: ConciergeRequest) -> JSONResponse:
    result: dict[str, Any] = await run(payload)
    return JSONResponse(result)

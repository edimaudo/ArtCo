from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from models.schemas import ConciergeRequest
from services.concierge import run

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


@app.get("/taste", response_class=HTMLResponse)
async def taste_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="taste.html", context={"page": "taste"})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/concierge")
async def concierge(payload: ConciergeRequest) -> JSONResponse:
    result: dict[str, Any] = await run(payload)
    return JSONResponse(result)

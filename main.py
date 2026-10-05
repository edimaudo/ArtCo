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
    title="Art Concierge",
    description="A personal cultural discovery and artwork research concierge powered by Qloo.",
    version="0.2.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/concierge")
async def concierge(payload: ConciergeRequest) -> JSONResponse:
    result: dict[str, Any] = await run(payload)
    return JSONResponse(result)

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from models.schemas import ConciergeRequest


def encoded_payload(size: int = 96) -> str:
    return base64.b64encode(b"artco-test-image-" + b"x" * size).decode("ascii")


def test_concierge_request_accepts_compressed_inspiration_image():
    payload = {
        "intent": "discover",
        "goal": "Find art with this mood and palette.",
        "inspiration_image_ids": ["image-1"],
        "inspiration_images": [{
            "image_id": "image-1",
            "name": "abstract-colours.jpg",
            "mime_type": "image/jpeg",
            "data": encoded_payload(),
        }],
    }
    parsed = ConciergeRequest.model_validate(payload)
    assert parsed.inspiration_image_ids == ["image-1"]
    assert len(parsed.inspiration_images) == 1
    assert parsed.inspiration_images[0].mime_type == "image/jpeg"


def test_concierge_request_rejects_unselected_image_id():
    with pytest.raises(ValidationError):
        ConciergeRequest.model_validate({
            "inspiration_image_ids": ["selected-image"],
            "inspiration_images": [{
                "image_id": "different-image",
                "mime_type": "image/jpeg",
                "data": encoded_payload(),
            }],
        })


def test_concierge_request_rejects_invalid_base64():
    with pytest.raises(ValidationError):
        ConciergeRequest.model_validate({
            "inspiration_image_ids": ["image-1"],
            "inspiration_images": [{
                "image_id": "image-1",
                "mime_type": "image/jpeg",
                "data": "not-valid-base64?bad!",
            }],
        })


def test_concierge_request_limits_number_of_inspiration_images():
    images = [
        {
            "image_id": f"image-{index}",
            "mime_type": "image/jpeg",
            "data": encoded_payload(),
        }
        for index in range(6)
    ]
    with pytest.raises(ValidationError):
        ConciergeRequest.model_validate({"inspiration_image_ids": [item["image_id"] for item in images], "inspiration_images": images})


def test_concierge_ui_has_image_upload_and_no_redundant_composer_labels():
    from fastapi.testclient import TestClient
    from main import app

    response = TestClient(app).get("/concierge")
    assert response.status_code == 200
    assert 'id="inspirationFiles"' in response.text
    assert 'id="browseInspiration"' not in response.text
    assert 'id="requestInput"' in response.text
    assert "Images are discarded when the search finishes." in response.text
    assert "I can help with" not in response.text
    assert "What would you like help with?" not in response.text


def test_fast_vision_response_can_inform_initial_catalogue_search(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import services.agent as agent
    from models.schemas import InspirationImage
    from services.llm import VisualAnalysis

    image = InspirationImage(
        image_id="image-vision",
        mime_type="image/jpeg",
        data=encoded_payload(),
    )

    async def fake_analyze(_request_text, _images):
        return VisualAnalysis(
            summary="Angular forms with a vivid palette.",
            visual_attributes=["angular geometric forms", "high contrast"],
            colour_palette=["navy", "gold"],
            search_terms=["geometric abstract painting"],
        )

    async def fake_extract(*_args, **_kwargs):
        return SimpleNamespace(
            cultural_references=[], art_interests=[], mediums=[], room=None,
            size_preference=None, market=None, budget_min=None, budget_max=None,
            number_of_works=None,
        )

    async def fake_search_entities(_query):
        return []

    async def fake_artist_insights(*_args, **_kwargs):
        return []

    async def fake_plan(*args, **_kwargs):
        observed["art_interests"] = args[3]
        return SimpleNamespace(queries=["geometric abstract painting"], rationale="Visual cues")

    async def fake_review(*args, **_kwargs):
        observed["review_context"] = args[1]
        return None

    monkeypatch.setattr(agent, "analyze_inspiration_images", fake_analyze)
    monkeypatch.setattr(agent, "extract_request_signals", fake_extract)
    monkeypatch.setattr(agent, "search_entities", fake_search_entities)
    monkeypatch.setattr(agent, "get_artist_insights", fake_artist_insights)
    monkeypatch.setattr(agent, "commercial_providers", lambda: [])
    monkeypatch.setattr(agent, "institutional_providers", lambda: [])

    result = asyncio.run(agent.run_concierge(ConciergeRequest(
        intent="discover",
        goal="Find art with this mood.",
        inspiration_image_ids=["image-vision"],
        inspiration_images=[image],
    )))

    assert "geometric abstract painting" in result["search_directions"]
    assert result["visual_inspiration"]["analyzed"] is True
    assert result["visual_inspiration"]["image_count"] == 1
    assert "high contrast" in result["visual_inspiration"]["visual_attributes"]


def test_api_accepts_image_payload_without_persisting_it_to_response(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    async def fake_run(payload):
        assert len(payload.inspiration_images) == 1
        return {
            "success": True,
            "intent": payload.intent.value,
            "results": [],
            "not_for_sale": [],
            "visual_inspiration": {"image_count": len(payload.inspiration_images), "analyzed": False},
        }

    monkeypatch.setattr(main, "run", fake_run)
    response = TestClient(main.app).post("/api/concierge", json={
        "intent": "discover",
        "goal": "Find art with this palette.",
        "inspiration_image_ids": ["image-1"],
        "inspiration_images": [{
            "image_id": "image-1",
            "name": "palette.jpg",
            "mime_type": "image/jpeg",
            "data": encoded_payload(),
        }],
    })
    assert response.status_code == 200
    assert response.json()["visual_inspiration"]["image_count"] == 1
    assert "data" not in response.text
    assert encoded_payload() not in response.text


def test_inspiration_images_are_request_only_and_old_browser_store_is_removed():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    js = (root / "static/js/app.js").read_text()
    concierge = (root / "templates/concierge.html").read_text()
    dashboard = (root / "templates/dashboard.html").read_text()
    assert "window.indexedDB.open(INSPIRATION_DB_NAME, 3)" in js
    assert "db.deleteObjectStore(INSPIRATION_STORE)" in js
    assert "Close other ArtCo tabs and reload to finish removing the old saved-image library." in js
    assert "await putInspirationImage(record)" not in js
    assert "getInspirationImagesByIds" not in js
    assert "putInspirationSetting('selectedIds', state.inspiration.selectedIds)" not in js
    assert "request.inspiration_images = await Promise.all" in js
    assert "sessionStorage.removeItem('artConciergePendingRequest')" in js
    assert "function stripInspirationPayload(request)" in js
    assert "delete safe.inspiration_images" in js
    assert 'id="browseInspiration"' not in concierge
    assert 'id="tasteInspirationGallery"' not in dashboard
    assert "data-use-inspiration" not in js

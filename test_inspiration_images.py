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


def test_selected_image_cannot_be_silently_omitted_from_request_payload():
    with pytest.raises(ValidationError, match="must be attached to the current request"):
        ConciergeRequest.model_validate({"inspiration_image_ids": ["image-1"]})


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
    assert 'id="inspirationLibrary"' not in response.text
    assert 'id="requestInput"' in response.text
    assert 'discarded when the search finishes' in response.text
    assert "I can help with" not in response.text
    assert "What would you like help with?" not in response.text


def test_image_search_runs_after_written_request_search_and_uses_visual_terms(monkeypatch):
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
    events = []

    class OrderedProvider:
        name = "Ordered collection"

        async def search(self, query: str, limit: int = 8):
            events.append(("catalogue_search", query))
            return []

    async def fake_analyze(_request_text, _images):
        events.append(("image_analysis", "started"))
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

    monkeypatch.setattr(agent, "analyze_inspiration_images", fake_analyze)
    monkeypatch.setattr(agent, "extract_request_signals", fake_extract)
    monkeypatch.setattr(agent, "search_entities", fake_search_entities)
    monkeypatch.setattr(agent, "get_artist_insights", fake_artist_insights)
    monkeypatch.setattr(agent, "commercial_providers", lambda: [])
    monkeypatch.setattr(agent, "institutional_providers", lambda: [OrderedProvider()])

    result = asyncio.run(agent.run_concierge(ConciergeRequest(
        intent="discover",
        goal="Find art with this mood.",
        inspiration_image_ids=["image-vision"],
        inspiration_images=[image],
    )))

    analysis_index = next(index for index, event in enumerate(events) if event[0] == "image_analysis")
    search_events = [(index, event[1]) for index, event in enumerate(events) if event[0] == "catalogue_search"]
    assert len(search_events) == 2, events
    assert search_events[0][0] < analysis_index < search_events[1][0], events
    assert search_events[0][1] == "Find art with this mood."
    assert search_events[1][1] == "geometric abstract painting"
    assert "geometric abstract painting" in result["search_directions"]
    assert result["visual_inspiration"]["analyzed"] is True
    assert result["visual_inspiration"]["search_completed"] is True
    assert result["visual_inspiration"]["image_count"] == 1
    assert "high contrast" in result["visual_inspiration"]["visual_attributes"]
    assert result["diagnostics"]["text_search_completed"] is True
    assert result["diagnostics"]["image_search_completed"] is True
    assert {item["search_stage"] for item in result["diagnostics"]["source_diagnostics"]} == {"text", "image"}


def test_image_analysis_failure_does_not_return_successful_text_only_result(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import services.agent as agent
    from models.schemas import InspirationImage
    from services.llm import InspirationImageAnalysisError

    image = InspirationImage(
        image_id="image-required",
        mime_type="image/jpeg",
        data=encoded_payload(),
    )
    events = []

    class TextProvider:
        name = "Text stage collection"

        async def search(self, query: str, limit: int = 8):
            events.append(("text_search", query))
            return []

    async def fake_analyze(_request_text, _images):
        events.append(("image_analysis", "failed"))
        return None

    async def fake_extract(*_args, **_kwargs):
        return SimpleNamespace(
            cultural_references=[], art_interests=[], mediums=[], room=None,
            size_preference=None, market=None, budget_min=None, budget_max=None,
            number_of_works=None,
        )

    monkeypatch.setattr(agent, "analyze_inspiration_images", fake_analyze)
    monkeypatch.setattr(agent, "extract_request_signals", fake_extract)
    monkeypatch.setattr(agent, "search_entities", lambda _query: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(agent, "get_artist_insights", lambda *_args, **_kwargs: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(agent, "commercial_providers", lambda: [])
    monkeypatch.setattr(agent, "institutional_providers", lambda: [TextProvider()])

    async def run_test():
        with pytest.raises(InspirationImageAnalysisError, match="image search is required"):
            await agent.run_concierge(ConciergeRequest(
                intent="discover", goal="Find calm abstract art",
                inspiration_image_ids=["image-required"], inspiration_images=[image],
            ))

    asyncio.run(run_test())
    assert events[0][0] == "text_search"
    assert events[-1] == ("image_analysis", "failed")


def test_api_returns_clear_failure_when_required_image_analysis_fails(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from services.llm import InspirationImageAnalysisError

    async def fake_run(_payload):
        raise InspirationImageAnalysisError("The image search is required. Configure visual analysis and retry.")

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
    assert response.status_code == 503
    assert response.json()["success"] is False
    assert "image search is required" in response.json()["detail"]


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

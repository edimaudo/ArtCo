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
    assert 'id="inspirationLibrary"' not in response.text
    assert 'id="requestInput"' in response.text
    assert 'discarded when the search finishes' in response.text
    assert "I can help with" not in response.text
    assert "What would you like help with?" not in response.text


def test_image_search_runs_after_written_search_and_uses_visual_terms(monkeypatch):
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

    async def fake_analyze(_request_text, _images):
        events.append("image_analysis")
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

    async def fake_research(directions, _request, _status, *, phase_label, **_kwargs):
        events.append((phase_label, list(directions)))
        return [], []

    monkeypatch.setattr(agent.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(agent, "analyze_inspiration_images", fake_analyze)
    monkeypatch.setattr(agent, "extract_request_signals", fake_extract)
    monkeypatch.setattr(agent, "search_entities", fake_search_entities)
    monkeypatch.setattr(agent, "get_artist_insights", fake_artist_insights)
    monkeypatch.setattr(agent, "_research", fake_research)

    result = asyncio.run(agent.run_concierge(ConciergeRequest(
        intent="discover",
        goal="Find art with this mood.",
        inspiration_image_ids=["image-vision"],
        inspiration_images=[image],
    )))

    assert events[0][0] == "Searching your written request"
    assert events[1] == "image_analysis"
    assert events[2][0] == "Searching for artwork that matches your images"
    assert "geometric abstract painting" in result["search_directions"]
    assert result["visual_inspiration"]["analyzed"] is True
    assert result["visual_inspiration"]["image_count"] == 1
    assert "high contrast" in result["visual_inspiration"]["visual_attributes"]
    assert result["diagnostics"]["image_search"]["performed"] is True
    assert "geometric abstract painting" in result["diagnostics"]["image_search"]["directions"]


def test_attached_image_analysis_failure_does_not_return_text_only_results(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import services.agent as agent
    from models.schemas import InspirationImage

    image = InspirationImage(
        image_id="image-failed",
        mime_type="image/jpeg",
        data=encoded_payload(),
    )
    calls = []

    async def fake_analyze(_request_text, _images):
        calls.append("image_analysis")
        return None

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

    async def fake_research(_directions, _request, _status, *, phase_label, **_kwargs):
        calls.append(phase_label)
        return [], []

    monkeypatch.setattr(agent.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(agent, "analyze_inspiration_images", fake_analyze)
    monkeypatch.setattr(agent, "extract_request_signals", fake_extract)
    monkeypatch.setattr(agent, "search_entities", fake_search_entities)
    monkeypatch.setattr(agent, "get_artist_insights", fake_artist_insights)
    monkeypatch.setattr(agent, "_research", fake_research)

    result = asyncio.run(agent.run_concierge(ConciergeRequest(
        intent="discover",
        goal="Find art with this mood.",
        inspiration_image_ids=["image-failed"],
        inspiration_images=[image],
    )))

    assert calls == ["Searching your written request", "image_analysis"]
    assert result["success"] is False
    assert result["error_type"] == "RequiredVisualSearchFailed"
    assert "not returned text-only recommendations" in result["detail"]
    assert result["results"] == []


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


def test_learn_about_art_hides_image_uploader_and_exposes_written_topic_guidance():
    from pathlib import Path
    from fastapi.testclient import TestClient
    from main import app

    response = TestClient(app).get("/concierge?intent=learn")
    assert response.status_code == 200
    assert 'id="visualInspirationSection" class="visual-inspiration" aria-labelledby="inspirationHeading" hidden' in response.text
    js = (Path(__file__).resolve().parent / "static/js/app.js").read_text()
    assert "Image uploads are not used in this mode" in js
    assert "Enter an artist, artwork title, movement, technique, or idea to learn about." in js


def test_concierge_clears_stale_native_validation_and_uses_dropdown_intent():
    from pathlib import Path

    js = (Path(__file__).resolve().parent / "static/js/app.js").read_text()
    assert "input?.addEventListener('input', () =>" in js
    assert "if (input.value.trim()) input.setCustomValidity('')" in js
    assert "if (form) form.noValidate = true;" in js
    assert "const clean = String(input?.value ?? prompt ?? '').trim()" in js
    assert "const inferred = state.intent;" in js
    assert "const learnNeedsTopic = state.intent === 'learn';" in js


def test_learning_guide_succeeds_and_ignores_any_attached_images(monkeypatch):
    import asyncio
    from pathlib import Path

    import services.agent as agent
    from models.schemas import InspirationImage

    image = InspirationImage(
        image_id="image-learning",
        name="Tsumari in Bloom by Yayoi Kusama.jpg",
        mime_type="image/jpeg",
        data=encoded_payload(),
    )

    async def unexpected_vision_call(*_args, **_kwargs):
        raise AssertionError("Learn about art must not require or call image interpretation")

    monkeypatch.setattr(agent.config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(agent, "analyze_inspiration_images", unexpected_vision_call)
    result = asyncio.run(agent.run_concierge(ConciergeRequest(
        intent="learn",
        goal="Tsumari in Bloom by Yayoi Kusama: explain the artwork, artist, context, and what to look for.",
        inspiration_image_ids=["image-learning"],
        inspiration_images=[image],
    )))

    assert result["success"] is True
    assert result["intent"] == "learn"
    assert result["learning_guide"]["topic"].startswith("Tsumari in Bloom by Yayoi Kusama")
    assert result["results"] == []
    assert result["diagnostics"]["catalogue_search_performed"] is False
    assert result["diagnostics"]["image_analysis_required"] is False
    assert result["diagnostics"]["image_analysis_performed"] is False
    assert result["diagnostics"]["images_ignored_for_learning"] is True
    assert result["visual_inspiration"]["image_count"] == 0

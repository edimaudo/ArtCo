"""The Concierge always requires written input, including for image requests."""

import pytest


@pytest.mark.parametrize("intent", ["discover", "find", "curate", "learn"])
def test_image_alone_cannot_submit_any_concierge_mode(intent, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    async def unexpected_run(_payload):
        raise AssertionError("Concierge execution must not start without written input")

    monkeypatch.setattr(main, "run", unexpected_run)
    response = TestClient(main.app).post("/api/concierge", json={
        "intent": intent,
        "inspiration_image_ids": ["image-1"],
        "inspiration_images": [{
            "image_id": "image-1",
            "mime_type": "image/jpeg",
            "data": "YXJ0" + "eA" * 20,
        }],
    })
    assert response.status_code == 422
    assert response.json()["error_type"] == "WrittenPromptRequired"
    assert "cannot replace the text" in response.json()["detail"]


@pytest.mark.parametrize("goal", [None, "", "   ", "\n\t"])
def test_blank_or_missing_written_prompt_is_rejected_even_with_image(goal, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    async def unexpected_run(_payload):
        raise AssertionError("Concierge execution must not start without written input")

    monkeypatch.setattr(main, "run", unexpected_run)
    payload = {
        "intent": "discover",
        "goal": goal,
        "inspiration_image_ids": ["image-1"],
        "inspiration_images": [{
            "image_id": "image-1",
            "mime_type": "image/jpeg",
            "data": "YXJ0" + "eA" * 20,
        }],
    }
    response = TestClient(main.app).post("/api/concierge", json=payload)
    assert response.status_code == 422
    assert response.json()["error_type"] == "WrittenPromptRequired"


def test_text_prompt_is_trimmed_and_can_be_combined_with_an_image(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    observed = {}

    async def fake_run(payload):
        observed["goal"] = payload.goal
        return {"success": True, "intent": payload.intent.value, "results": [], "not_for_sale": []}

    monkeypatch.setattr(main, "run", fake_run)
    response = TestClient(main.app).post("/api/concierge", json={
        "intent": "discover",
        "goal": "  Explore similar art works of Yayoi Kusama.  ",
        "inspiration_image_ids": ["image-1"],
        "inspiration_images": [{
            "image_id": "image-1",
            "mime_type": "image/jpeg",
            "data": "YXJ0" + "eA" * 20,
        }],
    })
    assert response.status_code == 200
    assert observed["goal"] == "Explore similar art works of Yayoi Kusama."


def test_frontend_requires_text_even_if_images_are_attached():
    from pathlib import Path

    js = (Path(__file__).parent / "static" / "js" / "app.js").read_text()
    begin = js.index("async function beginRequest(prompt)")
    end = js.index("function beginNotForMeSearch", begin)
    body = js[begin:end]
    assert "if (!clean)" in body
    assert "Add a short description or at least one inspiration image." not in body
    assert "Inspiration images can support your request but cannot replace the text." in body

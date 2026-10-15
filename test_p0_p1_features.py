from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

import main
import services.agent as agent
import services.payments as payments
from models.schemas import ConciergeRequest
from services.art_sources import Artwork
from services.payments import CheckoutSessionNotFound

ROOT = Path(__file__).resolve().parent


def artwork(id_: str, title: str, artist: str, *, medium: str = "painting", source_kind: str = "institution", availability: str = "not_for_sale", description: str = ""):
    return Artwork(
        id=id_, title=title, artist=artist, image_url=f"https://images.example.org/{id_}.jpg",
        detail_url=f"https://collection.example.org/{id_}", source="Test collection",
        source_kind=source_kind, availability=availability, price=None, currency=None,
        medium=medium, dimensions="50 x 70 cm", year="2020", description=description,
    )


def test_not_for_me_excludes_the_work_and_the_artist_server_side():
    works = [
        artwork("bad-1", "Orange Study", "Alex Doe"),
        artwork("bad-2", "Blue Study", "Alex Doe"),
        artwork("good-1", "Quiet Landscape", "Morgan Lee"),
    ]
    request = ConciergeRequest(
        intent="discover", goal="Find alternatives with a different visual direction",
        exclude_artwork_ids=["bad-1"], avoid_references=["Alex Doe"], number_of_works=3,
    )
    selected = agent._select(works, request, [], None)
    assert [work.id for work in selected] == ["good-1"]


def test_exclusion_ids_are_a_supported_concierge_request_field():
    request = ConciergeRequest.model_validate({
        "intent": "discover", "exclude_artwork_ids": ["work-a", "work-b"],
    })
    assert request.exclude_artwork_ids == ["work-a", "work-b"]


def test_relevance_scoring_uses_goal_metadata_and_intent_context():
    request = ConciergeRequest(intent="find", goal="Find geometric abstract sculpture", art_interests=["geometric abstract"], mediums=["sculpture"])
    relevant = artwork("relevant", "Geometric Study", "Artist A", medium="abstract bronze sculpture", description="geometric planes")
    unrelated = artwork("unrelated", "Garden Scene", "Artist B", medium="landscape oil painting", description="a quiet field")
    relevant_score = agent._deterministic_score(relevant, request, [], "contemporary art")
    unrelated_score = agent._deterministic_score(unrelated, request, [], "contemporary art")
    assert relevant_score > unrelated_score


def test_learning_guide_contains_real_topic_context_and_source_grounded_examples():
    guide = agent._build_learning_guide(
        "Explain abstract expressionism",
        [artwork("work-1", "Untitled", "A. Artist", medium="oil on canvas")],
    )
    assert guide["topic"] == "abstract expressionism"
    assert "1940s and 1950s" in guide["overview"]
    assert len(guide["key_ideas"]) >= 3
    assert len(guide["look_closer"]) >= 3
    assert guide["related_works"][0]["title"] == "Untitled"
    assert "not a claim that every one belongs" in guide["source_note"]


def test_learning_guide_page_is_connected_to_results_and_safe_rendering():
    html = (ROOT / "templates/results.html").read_text()
    js = (ROOT / "static/js/app.js").read_text()
    assert 'id="learningGuideSection"' in html
    assert 'id="learningKeyIdeas"' in html
    assert 'id="learningExamples"' in html
    assert "renderLearningGuide(data.learning_guide, intent)" in js
    assert "escapeHtml(work.title || 'Untitled')" in js
    assert "escapeAttribute(work.detail_url)" in js


def test_inspiration_images_only_live_in_the_current_search_handoff():
    js = (ROOT / "static/js/app.js").read_text()
    html = (ROOT / "templates/concierge.html").read_text()
    dashboard = (ROOT / "templates/dashboard.html").read_text()
    assert "window.indexedDB.open(INSPIRATION_DB_NAME, 3)" in js
    assert "db.deleteObjectStore(INSPIRATION_STORE)" in js
    assert "objectStore(INSPIRATION_STORE).put(record)" not in js
    assert "sessionStorage.removeItem('artConciergePendingRequest')" in js
    assert "delete historyRequest.inspiration_images" in js
    assert 'discarded when the search finishes' in html
    assert 'tasteInspirationGallery' not in dashboard
    assert 'browseInspiration' not in html


def test_not_for_me_ui_starts_a_fresh_search_instead_of_filling_refine_text():
    js = (ROOT / "static/js/app.js").read_text()
    assert "function beginNotForMeSearch(artwork, button = null)" in js
    assert "request.exclude_artwork_ids = [String(artwork.id || '')]" in js
    assert "beginNotForMeSearch(artwork, feedback)" in js
    assert "if (/^Not for me/i.test(text) && artwork)" in js


def test_checkout_status_endpoint_uses_verified_stripe_session(monkeypatch):
    async def fake_retrieve(session_id: str):
        assert session_id == "cs_test_123"
        return {
            "session_id": session_id, "status": "complete", "payment_status": "paid",
            "artwork_id": "work-1", "amount_total": 12500, "currency": "cad",
            "created": 1792000000, "expires_at": None,
        }

    monkeypatch.setattr(main, "retrieve_checkout_session", fake_retrieve)
    response = TestClient(main.app).get("/api/checkout/status?session_id=cs_test_123")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["payment_status"] == "paid"
    assert body["artwork_id"] == "work-1"
    assert "customer_details" not in body


def test_checkout_status_endpoint_handles_missing_session(monkeypatch):
    async def fake_retrieve(_session_id: str):
        raise CheckoutSessionNotFound("A valid Stripe Checkout Session ID is required.")

    monkeypatch.setattr(main, "retrieve_checkout_session", fake_retrieve)
    response = TestClient(main.app).get("/api/checkout/status")
    assert response.status_code == 404
    assert response.json()["success"] is False


def test_checkout_return_page_never_claims_payment_before_verification():
    client = TestClient(main.app)
    response = client.get("/checkout/success?session_id=cs_test_123")
    assert response.status_code == 200
    assert 'data-checkout-status="verify"' in response.text
    assert "the Checkout Session with Stripe" in response.text
    assert "Your checkout is complete." not in response.text
    assert "/static/js/app.js?v=20261015-p0p1" in response.text


def test_stripe_session_retrieval_uses_authenticated_stripe_api(monkeypatch):
    calls = {}
    monkeypatch.setattr(payments, "STRIPE_SECRET_KEY", "sk_test_fake")

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "cs_test_456", "status": "complete", "payment_status": "paid",
                "client_reference_id": "work-456", "metadata": {"artwork_id": "work-456"},
                "amount_total": 4200, "currency": "cad", "created": 1792000000,
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, auth):
            calls["url"] = url
            calls["auth"] = auth
            return FakeResponse()

    monkeypatch.setattr(payments.httpx, "AsyncClient", FakeClient)
    session = asyncio.run(payments.retrieve_checkout_session("cs_test_456"))
    assert calls["url"] == f"{payments.STRIPE_API}/checkout/sessions/cs_test_456"
    assert calls["auth"] == ("sk_test_fake", "")
    assert session["payment_status"] == "paid"
    assert session["artwork_id"] == "work-456"

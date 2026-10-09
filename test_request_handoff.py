from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient
import main
from main import app

ROOT = Path(__file__).resolve().parent


def test_results_page_has_required_root():
    response = TestClient(app).get("/results?run=1")
    assert response.status_code == 200
    assert 'id="resultsPage"' in response.text
    assert 'id="artGrid"' in response.text


def test_request_is_persisted_before_navigation():
    js = (ROOT / "static/js/app.js").read_text()
    start = js.index("function beginRequest(")
    end = js.index("async function runPendingRequest(", start)
    body = js[start:end]
    assert body.index("sessionStorage.setItem('artConciergePendingRequest'") < body.index("window.location.assign(")
    assert "localStorage.setItem('artConciergePendingRequest'" in body


def test_pending_request_not_discarded_before_results_root_exists():
    js = (ROOT / "static/js/app.js").read_text()
    start = js.index("async function runPendingRequest(")
    end = js.index("function renderTasteSummary(", start)
    body = js[start:end]
    assert body.index("const resultsPage = $('#resultsPage')") < body.index("sessionStorage.removeItem('artConciergePendingRequest')")
    assert "fetch('/api/concierge'" in body


def test_artwork_card_checkout_id_is_initialized_before_use():
    js = (ROOT / "static/js/app.js").read_text()
    start = js.index("function renderCard(")
    end = js.index("function renderResults(", start)
    body = js[start:end]
    assert body.index("const id = escapeAttribute(work.id)") < body.index("const checkoutButton =")


def test_health_and_qloo_configuration():
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    diagnostics = client.get("/api/diagnostics").json()
    assert diagnostics["qloo"]["base_url"] == "https://hackathon.api.qloo.com"
    assert diagnostics["qloo"]["hackathon_environment"] is True


def test_concierge_api_returns_result_payload(monkeypatch):
    async def fake_run(payload):
        return {
            "success": True,
            "intent": payload.intent.value,
            "results": [{"id": "mock-artwork", "title": "Mock artwork"}],
            "not_for_sale": [],
            "total_found": 1,
        }

    monkeypatch.setattr(main, "run", fake_run)
    response = TestClient(main.app).post(
        "/api/concierge",
        json={"intent": "discover", "goal": "Find me contemporary photography", "loves": ["Radiohead"]},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "mock-artwork"


if __name__ == "__main__":
    test_results_page_has_required_root()
    test_request_is_persisted_before_navigation()
    test_pending_request_not_discarded_before_results_root_exists()
    test_artwork_card_checkout_id_is_initialized_before_use()
    print("request handoff checks: PASS (4/4)")

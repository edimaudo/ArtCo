from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

import main
import services.agent as agent
import services.payments as payments
from models.schemas import ConciergeRequest
from services.art_sources import Artwork, _map_artlogic, _map_artsy
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
    assert "/static/js/app.js?v=20261021-na-museum-three-results" in response.text


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


def test_purchase_history_is_integrated_with_checkout_and_dashboard():
    js = (ROOT / "static/js/app.js").read_text()
    dashboard = (ROOT / "templates/dashboard.html").read_text()
    assert "purchases: state.purchases" in js
    assert "state.purchases = Array.isArray(existing.purchases)" in js
    assert "async function recordCheckoutStarted(session, artwork)" in js
    assert "await recordCheckoutStarted(data, artwork)" in js
    assert "async function recordVerifiedPurchase(data)" in js
    assert "await recordVerifiedPurchase(data)" in js
    assert "async function checkTrackedPurchase(sessionId, button)" in js
    assert "data-purchase-session=" in js
    # Purchase tracking stays available to the checkout code but is hidden from the dashboard UI for now.
    assert 'id="dashboardPurchases"' not in dashboard
    assert 'id="purchasesTab"' not in dashboard
    assert 'id="purchasesPanel"' not in dashboard
    assert "dashboardTabs[(index + 1) % dashboardTabs.length]" in js
    assert "No purchase checkouts have been started in this browser." not in dashboard
    assert "Review checkouts started in this browser" not in dashboard
    assert "Shipping and delivery updates come from the seller, not ArtCo." not in dashboard
    assert dashboard.index('id="tasteTab"') < dashboard.index('id="overviewPanel"')


def test_concierge_intents_keep_distinct_ranking_or_selection_rules():
    geometric = artwork("geometric", "Geometric Study", "Aligned Artist", medium="geometric abstract sculpture", description="geometric forms")
    quiet = artwork("quiet", "Quiet Landscape", "Other Artist", medium="oil landscape", description="quiet landscape")

    # Discover uses its exploration setting to favour new directions; Find ranks against the actual brief.
    discovery_request = ConciergeRequest(intent="discover", goal="Find new work", art_interests=["geometric abstract"], discovery_level=90)
    adventurous = agent._deterministic_score(geometric, discovery_request, [], "experimental contemporary art")
    familiar = agent._deterministic_score(geometric, discovery_request, [], "geometric abstract art")
    assert adventurous > familiar

    find_request = ConciergeRequest(intent="find", goal="Find geometric abstract sculpture", art_interests=["geometric abstract"], mediums=["sculpture"])
    assert agent._deterministic_score(geometric, find_request, [], "contemporary art") > agent._deterministic_score(quiet, find_request, [], "contemporary art")

    # Taste discovery receives a positive relevance signal for a Qloo-related artist.
    taste_request = ConciergeRequest(intent="taste", goal="Explore my taste")
    aligned_qloo = agent._deterministic_score(geometric, taste_request, [{"name": "Aligned Artist"}], "contemporary art")
    unaligned_qloo = agent._deterministic_score(geometric, taste_request, [{"name": "Different Artist"}], "contemporary art")
    assert aligned_qloo > unaligned_qloo

    # Curate favours artist variety instead of returning several works by one artist.
    curated = [
        artwork("a1", "One", "Same Artist", source_kind="commercial", availability="available"),
        artwork("a2", "Two", "Same Artist", source_kind="commercial", availability="available"),
        artwork("b1", "Three", "Different Artist", source_kind="commercial", availability="available"),
    ]
    curated_result = agent._select(curated, ConciergeRequest(intent="curate", goal="Curate a set", number_of_works=2), [], None)
    assert len(curated_result) == 2
    assert len({item.artist for item in curated_result}) == 2

    # Buy remains a hard availability/source filter.
    buy_works = [
        artwork("institution-work", "Institutional", "Museum Artist"),
        artwork("available-work", "For Sale", "Gallery Artist", source_kind="commercial", availability="available"),
        artwork("unavailable-work", "Unavailable", "Other Gallery", source_kind="commercial", availability="not_for_sale"),
    ]
    bought = agent._select(buy_works, ConciergeRequest(intent="buy", goal="Buy art", purchase_required=True, number_of_works=2), [], None)
    assert [item.id for item in bought] == ["available-work"]

    # Learn prioritizes institutional context and descriptive records.
    learn_request = ConciergeRequest(intent="learn", goal="Learn about a movement")
    institutional = artwork("museum", "Study", "Artist", source_kind="institution", description="Historical context")
    commercial = artwork("gallery", "Study", "Artist", source_kind="commercial", availability="available", description="Historical context")
    assert agent._deterministic_score(institutional, learn_request, [], "art history") > agent._deterministic_score(commercial, learn_request, [], "art history")



def test_learn_intent_returns_guide_without_catalogue_recommendations(monkeypatch):
    async def unexpected_search(*args, **kwargs):
        raise AssertionError("Learn intent must not query art catalogues")

    monkeypatch.setattr(agent, "_research", unexpected_search)
    result = asyncio.run(agent.run_concierge(ConciergeRequest(intent="learn", goal="Explain impressionism")))
    assert result["success"] is True
    assert result["intent"] == "learn"
    assert result["results"] == []
    assert result["not_for_sale"] == []
    assert result["learning_guide"]["topic"] == "impressionism"
    assert result["learning_guide"]["related_works"] == []
    assert result["diagnostics"]["catalogue_search_performed"] is False


def test_only_works_with_concrete_image_urls_are_recommended():
    direct = artwork("with-image", "Study", "Artist")
    page_only = artwork("page-only", "Study", "Artist")
    page_only.image_url = None
    assert agent._has_image_candidate(direct) is True
    assert agent._has_image_candidate(page_only) is False


def test_learning_results_hide_recommendation_grid():
    html = (ROOT / "templates/results.html").read_text()
    js = (ROOT / "static/js/app.js").read_text()
    assert 'id="recommendationsSection"' in html
    assert 'id="refinePanel"' in html
    assert "if (recommendationSection) recommendationSection.hidden = learningOnly;" in js
    assert "if (refinePanel) refinePanel.hidden = learningOnly;" in js


def test_taste_editor_changes_are_draft_only_and_prompt_before_navigation():
    js = (ROOT / "static/js/app.js").read_text()
    html = (ROOT / "templates/dashboard.html").read_text()
    assert "state.tasteDraft.notes = event.target.value;" in js
    assert "state.tasteDraft.discoveryLevel = Number(event.target.value);" in js
    assert "function tasteDraftIsDirty()" in js
    assert "function requestTasteNavigation(action)" in js
    assert "$('#saveTasteAndContinue')?.addEventListener('click'" in js
    assert "$('#discardTasteAndContinue')?.addEventListener('click'" in js
    assert 'id="tasteUnsavedModal"' in html
    assert "Keep editing" in html
    css = (ROOT / "static/css/style.css").read_text()
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert ".taste-editor-lower .discovery-control" in css


def test_buy_research_uses_commercial_inventory_only(monkeypatch):
    calls = []

    class FakeProvider:
        def __init__(self, name):
            self.name = name

        async def search(self, query, limit=8):
            calls.append(self.name)
            return []

    commercial = [FakeProvider("Artsy"), FakeProvider("Collect24")]
    institutional = [FakeProvider("The Metropolitan Museum of Art"), FakeProvider("Cleveland Museum of Art")]
    monkeypatch.setattr(agent, "commercial_providers", lambda: commercial)
    monkeypatch.setattr(agent, "institutional_providers", lambda: institutional)
    monkeypatch.setattr(agent, "_provider_configured", lambda _provider: True)

    for intent in ("buy", "curate", "taste"):
        calls.clear()
        request = ConciergeRequest(intent=intent, purchase_required=(intent == "buy"), goal=f"{intent} an abstract painting", number_of_works=3)
        works, diagnostics = asyncio.run(agent._research(["abstract painting for sale"], request, []))
        assert works == []
        assert set(calls) == {"Artsy", "Collect24"}
        assert "The Metropolitan Museum of Art" not in calls
        assert all(item["provider"] not in {"The Metropolitan Museum of Art", "Cleveland Museum of Art"} for item in diagnostics)


def test_buy_empty_state_does_not_expose_backend_source_errors():
    js = (ROOT / "static/js/app.js").read_text()
    assert "No commercial art source is connected yet" not in js
    assert "Sources with connection issues:" not in js
    assert "No works explicitly listed as available to purchase matched this request." in js
    assert "No commercial seller inventory is connected right now." in js
    assert "const purchaseOnlyIntent = ['buy', 'curate', 'taste'].includes(intent) || usesSavedTaste;" in js


def test_new_results_run_never_restores_cached_previous_search():
    js = (ROOT / "static/js/app.js").read_text()
    start = js.index("function initResults()")
    end = js.index("function initSaved()", start)
    init = js[start:end]
    assert "const isNewRun = params.get('run') === '1'" in init
    assert init.index("if (isNewRun)") < init.index("else if (state.last)")
    run_start = js.index("async function runPendingRequest()")
    run_end = js.index("function renderTasteSummary", run_start)
    run = js[run_start:run_end]
    assert "if (tasteSummary) tasteSummary.hidden = true" in run
    assert "if (visualSummary) { visualSummary.hidden = true; visualSummary.innerHTML = ''; }" in run
    assert "if (notForSaleGrid) notForSaleGrid.innerHTML = ''" in run


def test_artsy_inquiry_only_items_are_not_purchase_inventory():
    inquiry_only = _map_artsy({
        "id": "inquiry-1", "title": "Inquiry only", "can_acquire": "false",
        "can_inquire": True, "sold": False,
        "_embedded": {"artists": [{"name": "Gallery Artist"}]},
        "_links": {"thumbnail": {"href": "https://images.example.org/work.jpg"},
                   "permalink": {"href": "https://www.artsy.net/artwork/inquiry-1"}},
    })
    buyable = _map_artsy({
        "id": "buyable-1", "title": "Buyable", "can_acquire": True,
        "can_inquire": False, "sold": False,
        "_embedded": {"artists": [{"name": "Gallery Artist"}]},
        "_links": {"thumbnail": {"href": "https://images.example.org/work.jpg"},
                   "permalink": {"href": "https://www.artsy.net/artwork/buyable-1"}},
    })
    assert inquiry_only.availability == "not_for_sale"
    assert agent._is_purchasable(inquiry_only) is False
    assert buyable.availability == "available"
    assert agent._is_purchasable(buyable) is True


def test_artlogic_buy_availability_requires_explicit_for_sale_status():
    from_for_sale = _map_artlogic({"rows": [
        {"id": "for-sale", "title": "Available", "availability": "for_sale",
         "image_url": "https://gallery.example.org/available.jpg", "url": "https://gallery.example.org/available"},
        {"id": "published", "title": "Published but not marked for sale", "status": "published",
         "image_url": "https://gallery.example.org/published.jpg", "url": "https://gallery.example.org/published"},
        {"id": "reserved", "title": "Reserved", "availability": "available", "reserved": True,
         "image_url": "https://gallery.example.org/reserved.jpg", "url": "https://gallery.example.org/reserved"},
        {"id": "sold-conflict", "title": "Sold", "availability": "for_sale", "status": "sold",
         "image_url": "https://gallery.example.org/sold.jpg", "url": "https://gallery.example.org/sold"},
    ]})
    assert {work.id: work.availability for work in from_for_sale} == {
        "artlogic:for-sale": "available",
        "artlogic:published": "not_for_sale",
        "artlogic:reserved": "not_for_sale",
        "artlogic:sold-conflict": "not_for_sale",
    }


def test_curate_taste_and_buy_selection_only_returns_explicitly_buyable_inventory():
    buyable = artwork("buyable", "Blue Study", "Gallery Artist", source_kind="commercial", availability="available")
    unavailable = artwork("unavailable", "Unsold Study", "Other Artist", source_kind="commercial", availability="not_for_sale")
    museum = artwork("museum", "Collection Study", "Museum Artist", source_kind="institution", availability="not_for_sale")
    for intent in ("curate", "taste", "buy"):
        request = ConciergeRequest(intent=intent, goal=f"{intent} a blue study", number_of_works=3)
        selected = agent._select([buyable, unavailable, museum], request, [], None)
        assert [work.id for work in selected] == ["buyable"]


def test_develop_my_taste_uses_saved_work_notes_and_previous_ideas():
    from models.schemas import ExistingArtworkReference
    request = ConciergeRequest(
        intent="taste",
        goal="Help me expand my taste",
        profile_notes="Bold geometry, tactile surfaces, and rust orange",
        existing_ideas=["Art that feels like live punk music"],
        existing_artworks=[ExistingArtworkReference(
            title="Geometric Study", artist="Mara Example", medium="mixed media", description="Layered angular forms"
        )],
    )
    directions = agent._taste_reference_directions(request)
    assert "Bold geometry, tactile surfaces, and rust orange" in directions
    assert "Art that feels like live punk music" in directions
    assert "Geometric Study Mara Example mixed media" in directions

    work = artwork("related", "Geometric Study No. 2", "Mara Example", medium="mixed media", description="Layered angular forms", source_kind="commercial", availability="available")
    with_references = agent._deterministic_score(work, request, [], "contemporary art")
    without_references = agent._deterministic_score(work, ConciergeRequest(intent="taste", goal="Help me expand my taste"), [], "contemporary art")
    assert with_references > without_references


def test_frontend_sends_saved_artworks_and_ideas_only_when_personalization_is_enabled():
    js = (ROOT / "static/js/app.js").read_text()
    html = (ROOT / "templates/concierge.html").read_text()
    assert "personalize_with_saved_taste: useSavedTaste" in js
    assert "existing_artworks: useSavedTaste" in js
    assert "existing_ideas: useSavedTaste" in js
    assert "profile_notes: useSavedTaste" in js
    assert "state.saved.slice(0, 6).map((work) => ({" in js
    assert "Use my saved taste" in html
    assert "const purchaseOnlyIntent = ['buy', 'curate', 'taste'].includes(intent) || usesSavedTaste;" in js


def test_buy_api_and_ui_never_pad_with_non_purchasable_works():
    agent_source = (ROOT / "services/agent.py").read_text()
    js = (ROOT / "static/js/app.js").read_text()
    assert '"not_for_sale": [] if purchase_only_intent' in agent_source
    assert "work.source_kind === 'commercial' && work.availability === 'available'" in js
    assert "const notForSale = [];" in js
    assert "museum collection records are not substituted" in js


def test_fresh_search_invalidates_old_state_and_refreshes_bfcache_handoff():
    js = (ROOT / "static/js/app.js").read_text()
    assert "function resultsRunUrl(intent, fromConcierge = false, personalizeWithSavedTaste = state.personalizeExplore)" in js
    assert "run_id: runId" in js
    assert "state.searchRunActive = true;" in js
    assert "state.last = null;" in js
    assert "if (state.searchRunActive) return;" in js
    assert "window.addEventListener('pageshow', (event) =>" in js
    assert "if (sessionStorage.getItem('artConciergePendingRequest'))" in js


def test_taste_lower_fields_have_shared_label_height_and_top_alignment():
    css = (ROOT / "static/css/style.css").read_text()
    assert ".taste-editor-lower {" in css
    assert "height: 3.1rem;" in css
    assert "min-height: 3.1rem;" in css
    assert ".taste-editor-lower > div { align-self: stretch; min-width: 0; }" in css


def test_concierge_exposes_four_clear_modes_and_hides_buy_service_choice():
    html = (ROOT / "templates/concierge.html").read_text()
    home = (ROOT / "templates/index.html").read_text()
    assert '<option value="discover">Explore art</option>' in html
    assert '<option value="find">Find art</option>' in html
    assert '<option value="curate">Curate a collection</option>' in html
    assert '<option value="learn">Learn about art</option>' in html
    assert '<option value="buy">Buy art</option>' not in html
    assert '<option value="taste">' not in html
    assert 'id="personalizeExplore"' in html
    assert 'Develop my taste</strong>' not in home
    assert home.count('class="service-card"') == 4
    assert 'intent=buy' not in home
    assert 'Choose a service. Your concierge keeps the context as you return.' not in home


def test_concierge_heading_dropdown_and_dynamic_submit_label_have_distinct_roles():
    html = (ROOT / "templates/concierge.html").read_text()
    js = (ROOT / "static/js/app.js").read_text()
    assert '<h1 id="conciergeTitle">Your art concierge</h1>' in html
    assert "Choose how you'd like to work with art, then describe what you have in mind." in html
    assert '<label for="serviceSelect">What would you like to do?</label>' in html
    assert 'id="serviceDescription" class="service-description"' in html
    assert 'id="askConciergeLabel"' in html
    assert 'id="requestHint"' not in html
    assert 'Add as much or as little detail as you like.' in html
    assert 'Upload artwork, patterns, colours. These images are discarded when the search finishes.' in html
    assert "const [serviceTitle, description, promptPlaceholder] = intentCopy(state.intent);" in js
    assert r"if (askLabel) askLabel.textContent = String(serviceTitle).trim().split(/\s+/)[0] || 'Explore';" in js
    assert "if (descriptionNode) descriptionNode.textContent = description;" in js


def test_saved_artworks_cards_show_full_institution_name_after_artist_and_balance_actions():
    js = (ROOT / "static/js/app.js").read_text()
    css = (ROOT / "static/css/style.css").read_text()
    assert 'class="art-location" title="${escapeAttribute(source)}"' in js
    assert 'state.saved.map((work) => renderCard(work, { savedLayout: true }))' in js
    assert 'class="art-meta"${meta ?' in js and ' hidden' in js
    assert ".saved-art-card .art-location" in css
    assert "white-space: normal;" in css
    assert "text-overflow: clip;" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert ".art-actions .card-action" in css


def test_concierge_spacing_is_compact_enough_to_keep_submit_action_visible():
    css = (ROOT / "static/css/style.css").read_text()
    assert ".concierge-page .concierge-header .lede" in css
    assert ".concierge-page .conversation-panel { margin-top: 18px; }" in css
    assert ".concierge-page .conversation-history { margin-bottom: 8px; }" in css
    assert ".concierge-page .concierge-service-control { margin-bottom: 12px; }" in css
    assert "height: 156px;" in css and "min-height: 156px;" in css
    assert ".concierge-page .visual-inspiration" in css


def test_personalized_explore_uses_saved_references_and_enforces_purchase_only():
    from models.schemas import ExistingArtworkReference
    request = ConciergeRequest(
        intent="discover",
        personalize_with_saved_taste=True,
        goal="Explore art like my saved geometric pieces",
        profile_notes="Bold geometry and rust orange",
        existing_ideas=["Art that feels like punk music"],
        existing_artworks=[ExistingArtworkReference(title="Geometric Study", artist="Mara Example", medium="mixed media")],
    )
    assert request.purchase_required is True
    assert agent._uses_saved_taste(request)
    assert agent._requires_purchasable_inventory(request)
    directions = agent._taste_reference_directions(request)
    assert "Bold geometry and rust orange" in directions
    assert "Art that feels like punk music" in directions
    assert "Geometric Study Mara Example mixed media" in directions
    buyable = artwork("buyable", "Geometric Study No. 2", "Mara Example", source_kind="commercial", availability="available")
    museum = artwork("museum", "Geometric Study No. 3", "Museum Artist", source_kind="institution", availability="not_for_sale")
    assert [w.id for w in agent._select([buyable, museum], request, [], None)] == ["buyable"]


def test_legacy_taste_intent_is_supported_as_personalized_explore_compatibility():
    request = ConciergeRequest(intent="taste", goal="Build on my saved work")
    assert request.intent.value == "taste"
    assert agent._uses_saved_taste(request)
    assert agent._requires_purchasable_inventory(request)



def test_theme_is_applied_before_stylesheet_and_does_not_follow_os_color_scheme():
    base = (ROOT / "templates/base.html").read_text()
    css = (ROOT / "static/css/style.css").read_text()
    js = (ROOT / "static/js/app.js").read_text()
    assert base.index("localStorage.getItem('artcoThemePreference')") < base.index('rel="stylesheet"')
    assert ':root[data-theme="dark"]' in css
    assert 'color-scheme: light dark;' not in css
    assert "localStorage.setItem('artcoThemePreference', theme)" in js


def test_concierge_composer_has_reduced_vertical_spacing():
    css = (ROOT / "static/css/style.css").read_text()
    assert '.concierge-page .concierge-service-control { margin-bottom: 22px; }' in css
    assert 'min-height: 176px;' in css
    assert '.concierge-page .visual-inspiration {\n  margin-top: 20px;\n  padding-top: 18px;' in css
    assert '.concierge-page .composer-footer { margin-top: 10px; }' in css


def test_dashboard_taste_labels_share_the_same_fixed_top_alignment():
    css = (ROOT / "static/css/style.css").read_text()
    html = (ROOT / "templates/dashboard.html").read_text()
    assert 'id="tasteNotes"' in html and 'id="discoveryLevel"' in html
    assert '.dashboard-page .taste-editor-lower {\n  align-items: start !important;' in css
    assert '.dashboard-page .taste-editor-lower .field-label' in css
    assert 'height: 3.1rem !important;' in css
    assert 'align-items: flex-start !important;' in css


def test_learning_service_is_text_driven_and_never_searches_or_recommends_works(monkeypatch):
    image_payload = "YXJ0" + "eA" * 20
    async def forbidden_search(*_args, **_kwargs):
        raise AssertionError("Learn about art must not search artwork catalogues")

    monkeypatch.setattr(agent, "_research", forbidden_search)
    result = asyncio.run(agent.run_concierge(ConciergeRequest(
        intent="learn",
        goal="Tsumari in Bloom by Yayoi Kusama",
        inspiration_image_ids=["image-1"],
        inspiration_images=[{
            "image_id": "image-1", "mime_type": "image/jpeg", "data": image_payload,
        }],
    )))
    assert result["success"] is True
    assert result["learning_guide"]["topic"] == "Tsumari in Bloom by Yayoi Kusama"
    assert result["results"] == []
    assert result["total_found"] == 0

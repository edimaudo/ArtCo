import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from styletv import main, tvmaze


client = TestClient(main.app)


def demo_catalogue_payload():
    shows = []
    for source in main.SHOWS:
        shows.append({
            **source,
            "genres": ["Drama"],
            "rating": 7.5,
            "tvmaze_url": "https://www.tvmaze.com/",
            "data_source": "TVmaze",
        })
    return {
        "shows": shows,
        "catalogue": {
            "count": len(shows), "limit": 450, "source": "TVmaze",
            "source_url": "https://www.tvmaze.com/api", "license": "CC BY-SA",
            "source_status": "live",
        },
    }


@pytest.fixture
def stub_catalogue(monkeypatch):
    async def fake_get_show_catalog(_seed_shows):
        return demo_catalogue_payload()
    monkeypatch.setattr(main, "get_show_catalog", fake_get_show_catalog)


def tvmaze_record(show_id, name):
    return {
        "id": show_id,
        "name": name,
        "url": f"https://www.tvmaze.com/shows/{show_id}/test-series",
        "type": "Scripted",
        "language": "English",
        "genres": ["Drama"],
        "status": "Ended",
        "premiered": "2020-01-01",
        "rating": {"average": 7.5},
        "weight": 80,
        "network": {"name": "Example Network"},
        "image": {"medium": f"https://static.tvmaze.com/uploads/images/medium_portrait/{show_id}.jpg", "original": f"https://static.tvmaze.com/uploads/images/original_untouched/{show_id}.jpg"},
        "summary": f"<p>{name} follows a group of people through a dramatic story with retro clothing and music.</p>",
    }


def sign_in(client_instance=client):
    page = client_instance.get('/signin')
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match, 'CSRF hidden field not rendered'
    csrf = match.group(1)
    response = client_instance.post(
        '/signin',
        data={'email': 'demo@styletv.app', 'password': 'styleTV2026', 'csrf': csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client_instance


def test_landing_page_renders():
    response = client.get('/')
    assert response.status_code == 200
    assert 'The looks you love.' in response.text
    assert 'styleTV' in response.text


def test_app_redirects_to_signin_for_anonymous_user():
    anon = TestClient(main.app)
    response = anon.get('/app', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('/signin')


def test_demo_signin_catalog_and_signout_flow(stub_catalogue):
    authed = sign_in(TestClient(main.app))
    page = authed.get('/app')
    assert page.status_code == 200
    assert 'My style profile' in page.text

    config = authed.get('/api/config')
    assert config.status_code == 200
    assert config.json()['storage'] == 'IndexedDB (this browser)'

    catalog = authed.get('/api/catalog')
    assert catalog.status_code == 200
    payload = catalog.json()
    assert len(payload['shows']) >= 5
    assert len(payload['products']) >= 5
    assert all(item.get('shop_url') for item in payload['products'])
    assert all(item.get('try_on_reference_url') for item in payload['products'])

    signin_page = authed.get('/signout')
    assert signin_page.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', signin_page.text)
    assert match
    out = authed.post('/signout', data={'csrf': match.group(1)}, follow_redirects=False)
    assert out.status_code == 303
    assert '/signout?done=1' in out.headers['location']
    forbidden = authed.get('/api/catalog')
    assert forbidden.status_code == 401


def test_recommendations_are_grounded_in_catalogue(stub_catalogue, monkeypatch):
    authed = sign_in(TestClient(main.app))
    csrf = authed.get('/app').text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', csrf).group(1)
    monkeypatch.setattr(main, 'GEMINI_API_KEY', '')
    response = authed.post(
        '/api/recommendations',
        headers={'X-CSRF-Token': token},
        json={
            'query': 'punk plaid jacket under 100',
            'profile': {'favoriteShows': ['wednesday'], 'styleTags': ['punk', 'alternative'], 'patterns': ['plaid'], 'budget': 100},
            'limit': 4,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    catalog_ids = {item['id'] for item in main.PRODUCTS}
    assert 1 <= len(payload['products']) <= 4
    assert all(item['id'] in catalog_ids for item in payload['products'])
    assert payload['source'] == 'catalogue-matcher'


def test_yce_is_never_faked_when_key_is_missing(monkeypatch):
    authed = sign_in(TestClient(main.app))
    monkeypatch.setattr(main, 'YCE_API_KEY', '')
    app_page = authed.get('/app').text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', app_page).group(1)
    response = authed.post(
        '/api/color-analysis',
        headers={'X-CSRF-Token': token},
        files={'photo': ('selfie.jpg', b'not-a-real-jpeg', 'image/jpeg')},
        data={'consent': 'true'},
    )
    assert response.status_code == 503
    assert 'not configured' in response.json()['detail'].lower()


def test_explicit_query_budget_is_a_hard_ceiling():
    ranked = main.deterministic_rank(
        {"favoriteShows": [], "styleTags": ["punk"], "patterns": ["plaid"], "budget": 80},
        "punk-inspired jacket under $100",
    )
    assert ranked
    assert all(item["price"] <= 100 for item in ranked)


def test_explore_looks_query_prioritizes_selected_show():
    ranked = main.deterministic_rank({"favoriteShows": [], "budget": "any"}, "The Last of Us")
    assert ranked
    assert ranked[0]["id"] == "black-utility-jacket"


def test_yce_result_url_schema_is_recognized():
    payload = {"data": {"task_status": "success", "results": {"url": "https://example.test/generated.jpg"}}}
    assert main.extract_output_url(payload) == "https://example.test/generated.jpg"


def test_tvmaze_catalogue_is_capped_at_450_and_preserves_starter_shows(monkeypatch):
    tvmaze.clear_show_catalog_cache()

    index_records = []
    for page in tvmaze.TVMAZE_INDEX_PAGES:
        start_id = page * 250 + 1
        index_records.append([tvmaze_record(show_id, f"Indexed Series {show_id}") for show_id in range(start_id, start_id + 250)])

    async def fake_fetch_json(_client, url):
        if "/shows?page=" in url:
            requested_page = int(url.rsplit("=", 1)[1])
            return index_records[tvmaze.TVMAZE_INDEX_PAGES.index(requested_page)]
        if "/singlesearch/shows?q=" in url:
            title = url.rsplit("=", 1)[1].replace("%20", " ")
            seed = next((show for show in main.SHOWS if show["name"].casefold() == title.casefold()), None)
            if seed:
                return tvmaze_record(30000 + main.SHOWS.index(seed), seed["name"])
        return None

    monkeypatch.setattr(tvmaze, "_fetch_json", fake_fetch_json)
    result = asyncio.run(tvmaze.get_show_catalog(main.SHOWS))
    assert result["catalogue"]["count"] == 450
    assert result["catalogue"]["limit"] == 450
    assert len(result["shows"]) == 450
    assert len({show["id"] for show in result["shows"]}) == 450
    assert {show["id"] for show in result["shows"][:6]} == {show["id"] for show in main.SHOWS}
    assert all(show.get("image") for show in result["shows"] if show.get("data_source") == "TVmaze")
    assert all(show.get("tvmaze_url") for show in result["shows"])
    tvmaze.clear_show_catalog_cache()


def test_yce_paths_and_jpeg_metadata_match_provider_contract():
    assert main.YCE_TASK_PATHS["try-on"] == "/s2s/v2.0/task/cloth-v4"
    assert main.YCE_TASK_PATHS["color"] == "/s2s/v2.0/task/skin-tone-analysis"
    assert main.normalize_yce_content_type("image/jpeg") == "image/jpg"
    assert main.normalize_yce_content_type("image/jpg") == "image/jpg"
    assert main.normalize_yce_content_type("image/png") == "image/png"


def test_ineligible_standalone_lower_body_reference_is_not_sent_to_youcam(monkeypatch):
    authed = sign_in(TestClient(main.app))
    page = authed.get('/app').text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
    monkeypatch.setattr(main, "YCE_API_KEY", "")
    response = authed.post(
        "/api/try-on/wide-leg-denim",
        headers={"X-CSRF-Token": token},
        files={"photo": ("selfie.jpg", b"not-a-real-jpeg", "image/jpeg")},
        data={"consent": "true"},
    )
    assert response.status_code == 400
    assert "not eligible" in response.json()["detail"].lower()


def test_landing_hero_images_share_one_responsive_grid_row():
    from pathlib import Path
    template = Path("styletv/templates/landing.html").read_text()
    css = Path("public/assets/style.css").read_text()
    assert 'class="hero-art"' in template
    assert 'class="hero-image hero-image-main"' in template
    assert 'class="hero-image hero-image-side"' in template
    assert ".hero-art { position: relative; height: 485px;" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: minmax(0, 1fr)" in css
    assert ".hero-image-main, .hero-image-side { min-height: 0; margin-top: 0; }" in css


def test_try_on_uses_documented_clothes_v4_request(monkeypatch):
    authed = sign_in(TestClient(main.app))
    page = authed.get('/app').text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
    calls = {}

    async def fake_upload(_content, _filename, _content_type):
        return 'uploaded-source-id'

    async def fake_create_task(path, body):
        calls['path'] = path
        calls['body'] = body
        return {'data': {'task_id': 'task-demo-123'}}

    monkeypatch.setattr(main, 'YCE_API_KEY', 'test-key')
    monkeypatch.setattr(main, 'upload_image_to_yce', fake_upload)
    monkeypatch.setattr(main, 'create_yce_task', fake_create_task)
    response = authed.post(
        '/api/try-on/plaid-overshirt',
        headers={'X-CSRF-Token': token},
        files={'photo': ('person.jpg', b'valid-enough-for-mocked-upload', 'image/jpeg')},
        data={'consent': 'true'},
    )
    assert response.status_code == 200
    assert calls['path'] == '/s2s/v2.0/task/cloth-v4'
    assert calls['body']['src_file_id'] == 'uploaded-source-id'
    assert calls['body']['ref_file_url'] == main.get_product('plaid-overshirt')['try_on_reference_url']
    assert calls['body']['garment_category'] == 'outer'
    assert calls['body']['filter_multi_person'] == 'normal'


def test_color_analysis_uses_documented_skin_tone_task(monkeypatch):
    authed = sign_in(TestClient(main.app))
    page = authed.get('/app').text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
    calls = {}

    async def fake_upload(_content, _filename, _content_type):
        return 'uploaded-selfie-id'

    async def fake_create_task(path, body):
        calls['path'] = path
        calls['body'] = body
        return {'data': {'task_id': 'color-demo-123'}}

    monkeypatch.setattr(main, 'YCE_API_KEY', 'test-key')
    monkeypatch.setattr(main, 'upload_image_to_yce', fake_upload)
    monkeypatch.setattr(main, 'create_yce_task', fake_create_task)
    response = authed.post(
        '/api/color-analysis',
        headers={'X-CSRF-Token': token},
        files={'photo': ('selfie.jpg', b'valid-enough-for-mocked-upload', 'image/jpeg')},
        data={'consent': 'true'},
    )
    assert response.status_code == 200
    assert calls['path'] == '/s2s/v2.0/task/skin-tone-analysis'
    assert calls['body'] == {'src_file_id': 'uploaded-selfie-id', 'face_angle_strictness_level': 'high'}


def test_task_status_routes_poll_matching_youcam_v4_and_color_endpoints(monkeypatch):
    authed = sign_in(TestClient(main.app))
    requested_urls = []

    class FakeResponse:
        is_error = False

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requested_urls.append(url)
            if "skin-tone-analysis" in url:
                return FakeResponse({"data": {"task_status": "success", "results": {"color": {"skin_color": "#b9947c"}}}})
            return FakeResponse({"data": {"task_status": "success", "results": {"url": "https://cdn.example/try-on.jpg"}}})

    monkeypatch.setattr(main, "YCE_API_KEY", "test-key")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    try_on = authed.get('/api/tasks/try-on/task-123')
    color = authed.get('/api/tasks/color/color-123')
    assert try_on.status_code == 200 and try_on.json()['result_url'] == 'https://cdn.example/try-on.jpg'
    assert color.status_code == 200 and color.json()['colors']['skin_color'] == '#b9947c'
    assert requested_urls == [
        f"{main.YCE_BASE}/s2s/v2.0/task/cloth-v4/task-123",
        f"{main.YCE_BASE}/s2s/v2.0/task/skin-tone-analysis/color-123",
    ]


def test_non_seed_show_metadata_influences_garment_recommendations():
    show = {
        'id': 'tvmaze-99001',
        'name': 'The Alternative Series',
        'tags': ['punk', 'alternative'],
        'genres': ['Drama'],
        'tags_source': 'inferred',
    }
    ranked = main.deterministic_rank(
        {'favoriteShows': [], 'styleTags': [], 'patterns': [], 'colorPreferences': [], 'budget': 'any'},
        'The Alternative Series',
        shows=[show],
    )
    assert ranked
    assert ranked[0]['id'] == 'black-utility-jacket'


def test_try_on_dialog_follows_current_clothes_v4_full_body_guidance():
    from pathlib import Path
    app_template = Path('styletv/templates/app.html').read_text()
    assert 'high-resolution, front-facing full-body photo' in app_template
    assert 'showing the full body, with one person and a simple background' in app_template

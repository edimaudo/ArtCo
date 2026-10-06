# ArtCo 

## Product Design
A user-centric art concierge that combines broader cultural taste signals from Qloo with artwork research across commercial and institutional sources.

## The concierge roles

- **Discover**: find art beyond the obvious.
- **Find**: search for a specific brief.
- **Develop my taste**: learn through guided discovery.
- **Curate**: assemble works around a context.
- **Buy**: prioritize artwork that is actually available to acquire.
- **Learn about art**: understand an artist, artwork, movement, or idea.

## Architecture

The application uses a **native Python orchestration layer** with explicit tool-like service boundaries. 

The concierge loop is:

1. Resolve user-entered cultural references with Qloo `/search`.
2. Use Qloo `/v2/insights` to establish artist/taste directions.
3. Build an artwork search plan, optionally assisted by Gemini.
4. Search the appropriate artwork providers.
5. Evaluate candidates against the user's brief.
6. Broaden the search once when the initial results do not satisfy the objective.
7. Return a primary shortlist plus a clearly labeled **Not for sale** section when appropriate.

The Qloo implementation does not use the unsupported `/recommendations` or `/recs` endpoints.

## Artwork providers

### Commercial

**Artsy** is supported through an optional Partner API adapter. The application does not depend on the retiring public API. Configure `ARTSY_XAPP_TOKEN` and `ARTSY_PARTNER_ID` when partner access is available.

**Artlogic** is supported as an optional server-side feed adapter. Configure `ARTLOGIC_FEED_URL` only for a gallery/account that has authorized the feed.

**Collect24** is supported as an optional commercial registry adapter. It exposes public artwork records with explicit `for_sale` availability and public price fields. Configure `COLLECT24_API_KEY` server-side when access is available.

## Stack

- **Backend**: FastAPI
- **Front-end**: Jinja2 + Vanilla CSS + JS
- **AI**: Qloo API + Gemini SDK

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# set QLOO_API_KEY
uvicorn main:app --reload
```

Open http://127.0.0.1:8000








# ArtCo 

## Overview
A user-centric art concierge that combines broader cultural taste.

## The concierge roles
- **Explore art** — discover unexpected directions; optionally enable **Use my saved taste** to build on saved artworks, profile notes, likes/avoids, and recent ideas. Personalised Explore searches only commercial inventory explicitly available to acquire.
- **Find art** — search for a specific artist, artwork, style, subject, or medium.
- **Curate a collection** — assemble a coherent set using commercial works explicitly available to acquire.
- **Learn about art** — generate an educational guide from a required written topic. The image-inspiration uploader is hidden in this mode; enter the artwork title, artist, movement, technique, or idea. Learn does not run artwork catalogue searches or display artwork recommendation cards.


## Concierge intelligence

The concierge is implemented as native Python orchestration rather than a graph framework. Every request begins with a catalogue search based on the written brief and explicit preferences. When images are attached to Explore, Find, or Curate, ArtCo completes that written-request search first, then runs required image interpretation and a separate image-directed catalogue search before combining and ranking the candidates. Learn is text-only and ignores image payloads. Image-attached search requests receive a longer API deadline for their sequential phases; image analysis is not optional enrichment in those search modes. Qloo entity/insight enrichment remains best-effort. Artwork selection uses deterministic fit scoring, and hard constraints such as acquisition status and budget are enforced in Python.

Qloo usage follows the hackathon guidance: `/search` resolves known positive cultural references and `/v2/insights` supplies related artist entities and explainability; legacy `/recommendations` and `/recs` are not used. Qloo is a cultural-intelligence layer, not an artwork catalogue: actual works and image URLs always come from the artwork providers below.

To keep personalization meaningful, ArtCo does not send “things to avoid” or generic service prompts as positive Qloo interests. Visual subjects such as birds go to the image-analysis and artwork-search path, rather than being treated as unrelated cross-domain culture entities. When the response budget permits, an optional Gemini planning call translates Qloo's associations into visual-art query directions; those directions influence candidate ranking only when the artwork's own title, artist, medium, or description supports the connection. This bridge is best-effort and never delays the catalogue response.

## Architecture

The application uses a **native Python orchestration layer** with explicit tool-like service boundaries. No workflow framework is required.

For Explore, Find, and Curate, the concierge loop is:

1. Search artwork catalogues using the written brief and explicit art preferences.
2. If no inspiration images were attached, rank the written-search pool within the normal text-only deadline.
3. If images were attached, wait for the written search to finish, interpret the images with Gemini, and run a separate search using image-derived terms. If required image analysis cannot complete, return an explicit error rather than text-only recommendations.
4. Resolve positive cultural references using Qloo `/search` followed by `/v2/insights`; where available, use Qloo-derived directions as additional metadata-supported ranking signals.
5. Combine the written and, when attached, image-informed search pools, rank image-bearing works with deterministic fit scoring, enforce budget/acquisition rules, and return up to three visible cards. **Curate a collection, personalized Explore art, and Buy art search commercial seller inventory only** and show only listings explicitly marked available to acquire; they never fill gaps with museum records.

Learn follows a separate path: it requires a written topic, creates a learning guide, and makes no catalogue calls or artwork recommendations. The image-inspiration uploader is hidden in Learn, and direct or stale image payloads are ignored so a vision service error cannot block the educational output.

A slow optional enrichment is skipped rather than blocking the result. The API reports `diagnostics.duration_ms` to make live latency measurable.

The Qloo implementation does not use the unsupported `/recommendations` or `/recs` endpoints.

## Artwork providers

### Commercial

**Artsy** is supported through an optional Partner API adapter. The application does not depend on the retiring public API. Configure `ARTSY_XAPP_TOKEN` and `ARTSY_PARTNER_ID` when partner access is available. The adapter uses the partner search endpoint and follows artwork self-links; credentials are still required and are not bundled with ArtCo.

**Artlogic** is supported as an optional server-side feed adapter. Configure `ARTLOGIC_FEED_URL` only for a gallery/account that has authorized the feed.

**Collect24** is supported as an optional commercial registry adapter. It exposes public artwork records with explicit `for_sale` availability and public price fields. Configure `COLLECT24_API_KEY` server-side when access is available.

### Institutional

The MVP includes adapters for:

- Art Institute of Chicago
- The Metropolitan Museum of Art
- Cleveland Museum of Art
- Art Institute of Chicago (United States)
- The Metropolitan Museum of Art (United States)
- Cleveland Museum of Art (United States)

Institutional works are marked **NOT FOR SALE** and are used for discovery/context rather than purchase inventory.

## UI principles

The Concierge page uses a stable **Your art concierge** heading, a concise page-level instruction, a visible **What would you like to do?** selector, and a separate service-specific description. The selected service is not repeated as a second page title.


- Inter typography
- Light and dark themes
- Small / Medium / Large font scaling
- Responsive layout
- Keyboard-accessible controls
- Semantic form structure
- Editorial, gallery-like presentation rather than a chatbot interface

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# set QLOO_API_KEY
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`.


## Interface

The application is organized as separate screens rather than a single-page intake form:

- `/` — persistent home/workspace
- `/concierge` — conversational request workspace
- `/results` — researched artwork results and refinement
- `/saved` — saved works
- `/taste` — persistent taste references

The frontend keeps taste, saved works, recent requests, the latest result, purchase checkout references, and UI preferences in IndexedDB. Inspiration images are held in memory while composing a request, then passed through a one-time `sessionStorage` handoff and removed as soon as the results page consumes it. ArtCo does not use `localStorage` for persistent app data.

## Persistent user relationship

The current MVP keeps the concierge relationship in browser storage so a user can return to the same browser and retain their taste references, avoid references, saved works, recent requests, and latest result. This deliberately avoids introducing authentication or a database before account requirements are defined. The dashboard resumes prior requests by restoring the original brief into the concierge workspace.

## Commercial acquisition and payments

ArtCo treats commercial availability and payment as separate concerns. Artwork sources can expose a purchase/enquiry URL, which ArtCo opens directly for third-party inventory. ArtCo Checkout is enabled only for server-configured artwork IDs in `ARTCO_STRIPE_CHECKOUT_CATALOG`.

Stripe Checkout is used for one-time payments. For a third-party gallery or seller, configure a Stripe connected account and map the artwork ID to both a Stripe Price and the seller's connected account. ArtCo then creates a destination charge so the customer pays ArtCo and Stripe transfers the seller portion to the connected account. This configuration is appropriate only when ArtCo is operating the marketplace/merchant flow and has the required seller onboarding and commercial agreements.

Required variables for hosted checkout:

```text
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
ARTCO_BASE_URL=https://your-domain.example
ARTCO_STRIPE_CHECKOUT_CATALOG={"artco-direct:123":{"price_id":"price_..."}}
```

The `/api/stripe/webhook` endpoint verifies Stripe signatures and recognizes `checkout.session.completed`. Durable order/fulfillment storage should be added when the seller and fulfillment workflow are finalized.


## Product UX structure

ArtCo uses the Dashboard as the persistent home for returning users. The user's taste profile is managed inside the Dashboard rather than as a separate product destination. `/taste` redirects to `/dashboard#taste` for backwards compatibility.

Internal navigation is Dashboard, Concierge, and Saved. The landing page intentionally does not display product navigation.

## Service diagnostics

`GET /api/diagnostics` reports whether Qloo, Gemini, commercial artwork sources, and Stripe are configured. It never returns secret values.

Qloo hackathon requests are locked to `https://hackathon.api.qloo.com` so a hackathon API key cannot accidentally be sent to the production or staging base URL.

## Concierge results troubleshooting

The `/results` template must include the `id="resultsPage"` root element because the browser app checks for that root before it submits the pending request to `/api/concierge`. The request is stored before navigation and is only cleared after the page and payload are available.

For deployment troubleshooting:

- `GET /api/diagnostics` reports which keys/providers are configured and the Qloo base URL without revealing secrets.
- `GET /api/diagnostics/live` probes Qloo Search + Insights, Gemini request interpretation, and the public Art Institute, Met, and Cleveland collection APIs. It reports status/counts only.
- Set `GEMINI_MODEL=gemini-3.8-flash` for new deployments. If a `GEMINI_MODEL` override is already set in Vercel, update that environment variable too; changing `.env.example` does not override an existing deployment variable.

## Artwork images, result limits, and responsive search

- Discover/Find results are filtered to records with a usable HTTPS image URL before they are presented as recommendation cards.
- The Art Institute of Chicago adapter builds image URLs from its documented IIIF `image_id` values and requests public-domain works for image use.
- The Met adapter requests image-bearing objects and resolves their `primaryImageSmall`/`primaryImage` URLs.
- The Cleveland Museum of Art adapter uses the image asset URLs exposed in its open-access response.
- Museum searches are restricted to North America. This build has no validated Canadian provider that consistently returns image-bearing artwork records, so the current fallback uses only US museum collections: Art Institute of Chicago, The Met, and Cleveland Museum of Art. Non-North-American museum sources and Smithsonian have been removed.
- Each enabled provider is queried in parallel within each search phase, with a 3.25-second per-provider ceiling. Text-only requests retain a 4.6-second API deadline. Requests with attached images receive a 17.5-second deadline for the required sequence of written search, Gemini image interpretation, and a separate visual search.
- The current UI and API return at most three visible cards in total, including any **Not for sale** works. The count above the cards reports visible recommendations, not the number of candidates researched. `ARTWORK_RESULT_LIMIT` can lower that limit but cannot raise it above three.
- CSS and JavaScript asset URLs include a version query so deployed browsers do not keep using stale copies of the results cap or layout stylesheet.
- For allowlisted museum and known gallery-CDN hosts, ArtCo serves image bytes through a same-origin proxy. If a thumbnail fails, the browser tries the original artwork URL first, then an exact-work recovery search across public museum collections, and only then the official artwork page's image preview so generic Open Graph logos are less likely to replace the artwork. The preview endpoint tests multiple image candidates from the source page rather than stopping at the first failed candidate.
- HTTP thumbnail links are upgraded to HTTPS to avoid browser mixed-content blocking. The image proxy revalidates every redirect, recognizes valid image bytes even when a provider uses a generic binary content type, and caps each image response at 8 MB. Source-page image extraction remains restricted to trusted collection/gallery pages and approved image hosts.
- Artwork images use `object-fit: contain` so the artwork is not cropped to fill the card. Image responses are signature-checked and decoded as raster files before proxying; HTML pages, SVG logos, malformed image payloads, and tiny icon-sized files are rejected. Recommendations are selected only when a concrete image URL is present. Failed thumbnails retry the original source, an exact-work recovery search across public museum collections, and finally the source-page image preview; cards that still have no renderable image are removed rather than displayed without artwork.
- The API returns `diagnostics.duration_ms`; this is the application-side timing metric. End-to-end performance still depends on deployment cold starts and source availability.


## Visual inspiration images

- Users can attach up to five JPEG, PNG, or WebP images to a single Concierge search. The browser compresses them before submission.
- Inspiration images are request-only: they are kept in page memory while composing the brief, passed through a one-time `sessionStorage` handoff for navigation, and consumed by the results page before the API request. The handoff is removed before submission; image bytes are stripped from result history and recent-search data.
- The IndexedDB v3 migration removes the old persistent inspiration-image store and selected-image setting. Images cannot be reused from a gallery or recovered for a later search; they must be uploaded again when needed.
- `/api/concierge` receives only the images attached to that submitted request. With an image attached, ArtCo first completes the written-request search, then requires Gemini vision to derive visual descriptions and search terms, and finally performs a separate visual search. If Gemini is unconfigured, fails, times out, or produces no usable visual terms, the request fails explicitly instead of returning text-only recommendations. Remove the image to intentionally run a text-only search.
- Image bytes are sent to the configured model provider for analysis when a request includes them. They are not intentionally retained in ArtCo server-side storage by this implementation.

## Learn about art

- Learn requests return an educational guide only. They do not run catalogue searches or show artwork recommendations.
- The guide includes an overview, key ideas, looking prompts, vocabulary, and questions. Known topics include selected movements and artists; other topics receive a general critical-looking framework that avoids unsupported claims.
- If an image is attached to a Learn request, image interpretation is required and contributes to the guide. If interpretation fails, ArtCo returns an explicit error instead of ignoring the image.
- The guide is instructional context, not a substitute for a museum or other authoritative record. Artwork-specific dates, media, and provenance should be confirmed at the source.

## Checkout status

- ArtCo's success-return page does not assume that a browser redirect means payment succeeded. It queries `/api/checkout/status`, which retrieves the Checkout Session from Stripe using the server-side secret key.
- The UI distinguishes confirmed payment, checkout completed but unpaid/processing, expired checkout, and unconfirmed payment. It shows only a minimal session/artwork reference, payment status, and total.
- Before redirecting to Stripe, ArtCo records the checkout session, artwork reference, and visible title/artist in IndexedDB. After returning from Stripe, the app verifies the session and updates that browser-local record. The Dashboard's **Purchases** tab lists the most recent 12 checkout sessions and lets the user refresh payment status with Stripe.
- This history is available only in the same browser profile; it is not a server-side account ledger or cross-device order history. Shipping and fulfilment status are not tracked. Those require persistent order storage and a seller/fulfilment source; a Stripe Checkout Session reports payment/checkout state, not delivery status.

## Buy source configuration and diagnostics

Curate a collection, Explore art with **Use my saved taste** enabled, and Buy art search **commercial inventory only**. Museum collection APIs can supply records for general Explore and Find, but those records are not acquisition listings and are not queried by purchase-oriented modes. Learn about art generates a written-topic guide without querying collection APIs. Commercial source readiness is reported by `GET /api/diagnostics`; credentials and gallery feed URLs remain server-side.

- Artsy requires partner-approved access and both `ARTSY_XAPP_TOKEN` and `ARTSY_PARTNER_ID`.
- Artlogic requires an authorized gallery/account feed URL in `ARTLOGIC_FEED_URL`.
- Collect24 requires a server-side `COLLECT24_API_KEY`.
- Stripe checkout also requires a server-side secret and entries in `ARTCO_STRIPE_CHECKOUT_CATALOG` for artworks enabled for ArtCo checkout. External “Buy / enquire” links can only be shown when a commercial source provides a seller page.

No commercial inventory provider can be treated as connected simply because museum image APIs are available. Keep provider failures in backend diagnostics/logs; do not expose upstream HTTP errors in the customer-facing Buy panel.


### Buy inventory requirements

Curate a collection, personalised Explore, and Buy art are limited to connected commercial sellers and gallery feeds. They do not use institutional collection APIs as purchasable inventory. Artsy results are eligible only when `can_acquire` is true and the listing is not sold; Artlogic rows must explicitly declare a purchase-availability value; Collect24 rows must declare `for_sale`. Configure at least one of `ARTSY_XAPP_TOKEN` plus `ARTSY_PARTNER_ID`, `ARTLOGIC_FEED_URL`, or `COLLECT24_API_KEY` in the server environment. If no selling source is configured, the interface explains that no seller inventory is connected rather than showing museum collection records. When the user enables **Use my saved taste** in Explore art, ArtCo sends saved artwork metadata, saved profile notes, and recent ideas for that request and uses them in provider search directions and relevance scoring. The toggle also enforces commercial inventory explicitly available to acquire. Older `intent=taste` links remain supported and open Explore art with personalization enabled.

## Stack
- **Backend**: FastAPI
- **Front-end**: Jinja2 + Vanilla CSS + JS
- **AI**: Qloo API + Gemini SDK

## Next Steps
- Incorporate art studio data into the app focused on North America (US and Canada)
- Add a buy feature in my concierge using PayPal or Stripe --> **Buy**: prioritize artwork that is actually available to acquire.
  - It will also show purchase info in the dashboard






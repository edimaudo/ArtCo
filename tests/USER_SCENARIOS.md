# styleTV manual user scenarios

These are repeatable user-acceptance scenarios for the current demo catalogue. The catalogue is deliberately small; sample prices and shopping search links are not verified product listings.

## TS-01 — New user asks for help finding a look

**Enter:** “I'm new to dressing like people on TV. I like punk rock, black clothing and experimental patterns, but I don't want to look like I'm wearing a costume. Show me three looks I could actually wear, ideally under $100 each.”

**Expected:** Product matches come from the current catalogue, each with a style explanation, sample price, show-style cues, colour search options and retailer search link. Explicit budget wording acts as a ceiling. The app doesn't invent products or exact prices.

## TS-02 — Complete the style quiz

**Action:** Select alternative/punk styling, plaid and graphic prints, mostly dark colours, medium boldness and an $80 budget. Submit the quiz.

**Expected:** Answers persist in IndexedDB, recommendations refresh and the style profile shows the same choices. The user can retake the quiz.

## TS-03 — Search and favourite a show

**Enter:** Search “Wednesday”, add it to My shows, then remove it again.

**Expected:** Search filters the catalogue, the favourite state updates immediately after persistence, and recommendations refresh when shows are added or removed.

## TS-04 — Explore outfits from a specific show

**Action:** Open Explore looks on a show card that is not currently a favourite.

**Expected:** Show-linked garments receive priority. The app doesn't require the user to add the show as a favourite first.

## TS-05 — Discover shows by aesthetic

**Enter:** “I like alternative fashion, plaid and graphic prints. Recommend shows with similar wardrobe aesthetics that I haven't selected.”

**Expected:** The new-series area prioritizes Gemini-provided show IDs when Gemini is configured; otherwise, it uses the catalogue's style tags. Existing favourites are excluded from the discovery list.

## TS-06 — Change style preferences over time

**Enter:** “I'm bored of black and plaid. Keep Wednesday as a favourite, but add brighter colours and geometric patterns. My budget is still $100.”

**Expected:** Updating the profile changes matching behaviour, keeps the favourite show, preserves saved looks and applies the revised budget. Revisions don't require a new account.

## TS-07 — Explicit budget is respected

**Enter:** “Find me a punk-inspired jacket with a pattern for under $100.”

**Expected:** Returned sample products are priced at $100 or less. A query budget overrides the saved profile budget for that request; it does not change the saved budget unless the user edits the profile.

## TS-08 — Start a real YouCam virtual try-on

**Action:** Select an eligible garment, choose a suitable JPG/PNG photo, read and accept consent, and submit.

**Expected with `YCE_API_KEY`:** styleTV transfers the photo server-side to YouCam, starts Clothes V4, polls task status, then displays the returned result URL. It reports provider errors or timeouts without discarding the chosen item.

**Expected without `YCE_API_KEY`:** The UI explains how to configure the provider and confirms the photo was not uploaded. It must not show a fake completed result.

## TS-09 — Try-on rejects unsuitable inputs safely

**Action:** Attempt to submit without consent, with an unsupported file type, an image larger than 9.5 MB, or with no photo.

**Expected:** The request is blocked with a useful message. No provider upload is initiated before consent and file validation.

## TS-10 — Use facial colour-tone analysis

**Action:** In My style profile, select a clear front-facing JPG/PNG selfie, accept the separate consent and start analysis.

**Expected with `YCE_API_KEY`:** The app polls the YouCam Facial Color Tones Analyzer, shows returned facial colour hex values, and saves those colour notes—not the photo—in IndexedDB.

**Expected without `YCE_API_KEY`:** The user can continue normal discovery and sees an explanation that analysis is not configured. No claim of analysis is made.

## TS-11 — Colour ideas and shopping alternatives

**Action:** Select a colour swatch on a product card.

**Expected:** The retailer search link changes to look for alternatives in that colour. The UI explains that these are colour ideas; the current demo's VTO uses the base reference image until real, verified colour-variant images are added.

## TS-12 — Save and revisit a look

**Action:** Save a product, navigate elsewhere, reload the app, then open Saved looks and remove it.

**Expected:** The saved ID persists in this browser's IndexedDB across navigation and reload. The demo does not imply cross-device synchronization.

## TS-13 — Sign in and sign out

**Action:** Sign in with the demo credentials, edit the display name, sign out, then sign in again.

**Expected:** The server-side demo session ends on sign-out. Local preferences and saved look IDs remain in IndexedDB. The changed profile name remains after signing back in on the same browser.

## TS-14 — Accessibility and responsive checks

**Action:** Test keyboard-only navigation, focus visibility, upload controls, quiz choices, the VTO dialog, screen-reader labels, text size small/medium/large, light/dark mode, and a narrow mobile viewport.

**Expected:** Controls remain visible and operable; medium is the default; contrast stays adequate; users can close the dialog with its close button or Escape; reduced-motion preference is respected; and content doesn't require horizontal page scrolling at mobile widths.

## TS-15 — Gemini fallback and catalogue grounding

**Action:** Search in natural language with Gemini configured, then repeat with `GEMINI_API_KEY` unset or invalid.

**Expected:** When configured and successful, Gemini may rank only IDs present in the server's catalogue; retailer links and prices remain sourced from catalogue data. When unavailable, deterministic matching continues to work and reports that the fallback is in use.

(() => {
  const STATE_VERSION = 8;
  const initialQuery = new URLSearchParams(window.location.search);
  const requestedIntent = initialQuery.get('intent') || 'discover';
  const validIntents = ['discover', 'find', 'curate', 'learn'];
  const initialIntent = requestedIntent === 'taste' ? 'discover' : (validIntents.includes(requestedIntent) ? requestedIntent : 'discover');

  const state = {
    profile: { loves: [], avoids: [], notes: '', discoveryLevel: 50, updatedAt: null },
    tasteDraft: null,
    saved: [],
    recent: [],
    purchases: [],
    last: null,
    preferences: { theme: 'light', fontScale: 'md' },
    intent: initialIntent,
    personalizeExplore: requestedIntent === 'taste' || initialQuery.get('personalize_taste') === '1',
    inspiration: { images: [], preparing: false },
    searchRunActive: false,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const INSPIRATION_DB_NAME = 'artCoInspirationDB';
  const INSPIRATION_STORE = 'images'; // deleted by the v3 migration; never used for writes
  const INSPIRATION_SETTINGS_STORE = 'settings';
  const MAX_REQUEST_IMAGES = 5;
  const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;
  const MAX_COMPRESSED_BYTES = 360 * 1024;
  const activeInspirationUrls = new Set();
  let inspirationDbPromise = null;

  function openInspirationDb() {
    if (!('indexedDB' in window)) return Promise.reject(new Error('This browser does not support local image storage.'));
    if (inspirationDbPromise) return inspirationDbPromise;
    inspirationDbPromise = new Promise((resolve, reject) => {
      const request = window.indexedDB.open(INSPIRATION_DB_NAME, 3);
      request.onupgradeneeded = () => {
        const db = request.result;
        const transaction = request.transaction;
        // Migrate older ArtCo builds by deleting the persistent inspiration-image
        // store and its selected-image setting. Images are now request-only.
        if (db.objectStoreNames.contains(INSPIRATION_STORE)) db.deleteObjectStore(INSPIRATION_STORE);
        if (!db.objectStoreNames.contains(INSPIRATION_SETTINGS_STORE)) {
          db.createObjectStore(INSPIRATION_SETTINGS_STORE, { keyPath: 'key' });
        } else if (transaction) {
          transaction.objectStore(INSPIRATION_SETTINGS_STORE).delete('selectedIds');
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => { inspirationDbPromise = null; reject(new Error('Could not open local image storage.')); };
      request.onblocked = () => { inspirationDbPromise = null; reject(new Error('Close another ArtCo tab to finish updating local image storage.')); };
    });
    return inspirationDbPromise;
  }

  async function getInspirationSetting(key, fallback = null) {
    const db = await openInspirationDb();
    return new Promise((resolve, reject) => {
      const request = db.transaction(INSPIRATION_SETTINGS_STORE, 'readonly').objectStore(INSPIRATION_SETTINGS_STORE).get(key);
      request.onsuccess = () => resolve(request.result ? request.result.value : fallback);
      request.onerror = () => reject(new Error('Could not read the inspiration settings.'));
    });
  }

  async function putInspirationSetting(key, value) {
    const db = await openInspirationDb();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(INSPIRATION_SETTINGS_STORE, 'readwrite');
      transaction.objectStore(INSPIRATION_SETTINGS_STORE).put({ key, value });
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(new Error('Could not save the inspiration settings.'));
    });
  }

  function appStateSnapshot() {
    return {
      version: STATE_VERSION,
      profile: state.profile,
      saved: state.saved,
      recent: state.recent,
      purchases: state.purchases,
      last: state.last,
      preferences: state.preferences,
    };
  }

  function persistAppState() {
    return putInspirationSetting('appState', appStateSnapshot()).catch((error) => {
      console.warn('ArtCo could not persist app state in IndexedDB.', error);
    });
  }

  async function hydrateAppState() {
    try {
      const existing = await getInspirationSetting('appState', null);
      if (existing && typeof existing === 'object') {
        const profile = existing.profile || {};
        state.profile = {
          loves: Array.isArray(profile.loves) ? profile.loves.slice(0, 12) : [],
          avoids: Array.isArray(profile.avoids || profile.avoid_references) ? (profile.avoids || profile.avoid_references).slice(0, 12) : [],
          notes: String(profile.notes || ''),
          discoveryLevel: Number(profile.discoveryLevel ?? 50),
          updatedAt: profile.updatedAt || null,
        };
        state.saved = Array.isArray(existing.saved) ? existing.saved.slice(0, 40) : [];
        state.recent = Array.isArray(existing.recent) ? existing.recent.slice(0, 8) : [];
        state.purchases = Array.isArray(existing.purchases) ? existing.purchases.slice(0, 12) : [];
        state.last = existing.last || null;
        state.preferences = {
          theme: existing.preferences?.theme === 'dark' ? 'dark' : 'light',
          fontScale: ['sm', 'md', 'lg'].includes(existing.preferences?.fontScale) ? existing.preferences.fontScale : 'md',
        };
      } else {
        // App profile/results persist locally; user-supplied inspiration images do not.
        await putInspirationSetting('appState', appStateSnapshot());
      }
      state.inspiration.images = [];
      state.inspiration.preparing = false;
    } catch (error) {
      // Continue with the in-memory defaults if browser storage is unavailable.
      // No server account or cross-device sync is implied.
      console.warn('ArtCo could not load its browser library.', error);
    }
  }

  function clearInspirationObjectUrls() {
    activeInspirationUrls.forEach((url) => URL.revokeObjectURL(url));
    activeInspirationUrls.clear();
  }

  function imageObjectUrl(record) {
    const url = URL.createObjectURL(record.blob);
    activeInspirationUrls.add(url);
    return url;
  }

  function safeImageName(value) {
    const clean = String(value || 'Inspiration image').replace(/[\\/]+/g, ' ').trim();
    return clean.slice(0, 120) || 'Inspiration image';
  }

  function loadImageSource(file) {
    if (typeof window.createImageBitmap === 'function') {
      return window.createImageBitmap(file).then((bitmap) => ({
        image: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        cleanup: () => bitmap.close?.(),
      }));
    }
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => resolve({ image, width: image.naturalWidth, height: image.naturalHeight, cleanup: () => URL.revokeObjectURL(url) });
      image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('ArtCo could not open that image. Try a JPEG, PNG, or WebP file.')); };
      image.src = url;
    });
  }

  function canvasToBlob(canvas, type, quality) {
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error('This browser could not prepare the image for storage.')), type, quality);
    });
  }

  async function compressInspirationImage(file) {
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
      throw new Error(`${safeImageName(file.name)} is not a supported format. Use JPEG, PNG, or WebP.`);
    }
    if (file.size > MAX_UPLOAD_BYTES) throw new Error(`${safeImageName(file.name)} is larger than 12 MB.`);
    const source = await loadImageSource(file);
    try {
      if (!source.width || !source.height) throw new Error('ArtCo could not read the image dimensions.');
      let bestBlob = null;
      const canvas = document.createElement('canvas');
      const maxDimensions = [1280, 1080, 900, 760];
      const qualities = [0.82, 0.74, 0.66, 0.58, 0.5];
      for (const edge of maxDimensions) {
        const ratio = Math.min(1, edge / Math.max(source.width, source.height));
        canvas.width = Math.max(1, Math.round(source.width * ratio));
        canvas.height = Math.max(1, Math.round(source.height * ratio));
        const context = canvas.getContext('2d', { alpha: false });
        if (!context) throw new Error('Image processing is not available in this browser.');
        context.fillStyle = '#ffffff';
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(source.image, 0, 0, canvas.width, canvas.height);
        for (const quality of qualities) {
          const blob = await canvasToBlob(canvas, 'image/jpeg', quality);
          bestBlob = blob;
          if (blob.size <= MAX_COMPRESSED_BYTES) return { blob, width: canvas.width, height: canvas.height };
        }
      }
      if (!bestBlob || bestBlob.size > MAX_COMPRESSED_BYTES) {
        throw new Error(`${safeImageName(file.name)} could not be compressed enough. Try a smaller image.`);
      }
      return { blob: bestBlob, width: canvas.width, height: canvas.height };
    } finally {
      source.cleanup?.();
    }
  }

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const value = String(reader.result || '');
        const comma = value.indexOf(',');
        if (comma < 0) reject(new Error('Could not prepare the image for visual analysis.'));
        else resolve(value.slice(comma + 1));
      };
      reader.onerror = () => reject(new Error('Could not read the saved image for this request.'));
      reader.readAsDataURL(blob);
    });
  }

  function saveProfile() {
    state.profile.updatedAt = new Date().toISOString();
    persistAppState();
  }

  function cloneProfile(profile) {
    return {
      loves: [...(profile?.loves || [])],
      avoids: [...(profile?.avoids || [])],
      notes: String(profile?.notes || ''),
      discoveryLevel: Number(profile?.discoveryLevel ?? 50),
      updatedAt: profile?.updatedAt || null,
    };
  }

  function tasteComparable(profile) {
    return JSON.stringify({
      loves: [...(profile?.loves || [])],
      avoids: [...(profile?.avoids || [])],
      notes: String(profile?.notes || ''),
      discoveryLevel: Number(profile?.discoveryLevel ?? 50),
    });
  }

  function tasteDraftIsDirty() {
    return Boolean(state.tasteDraft && tasteComparable(state.tasteDraft) !== tasteComparable(state.profile));
  }

  function markTasteDraftChanged() {
    const updated = $('#tasteUpdated');
    if (updated) updated.textContent = tasteDraftIsDirty() ? 'Unsaved changes' : (state.profile.updatedAt ? `Updated ${new Date(state.profile.updatedAt).toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' })}` : 'Not yet saved');
  }

  async function saveTasteDraft() {
    if (!state.tasteDraft) state.tasteDraft = cloneProfile(state.profile);
    state.profile = { ...cloneProfile(state.tasteDraft), updatedAt: new Date().toISOString() };
    state.tasteDraft = cloneProfile(state.profile);
    await persistAppState();
    renderTastePage();
  }

  function discardTasteDraft() {
    state.tasteDraft = cloneProfile(state.profile);
    renderTastePage();
  }

  function addDraftReference(value) {
    const clean = String(value || '').trim().replace(/,$/, '');
    if (!state.tasteDraft) state.tasteDraft = cloneProfile(state.profile);
    if (!clean || state.tasteDraft.loves.some((item) => item.toLowerCase() === clean.toLowerCase()) || state.tasteDraft.loves.length >= 12) return;
    state.tasteDraft.loves.push(clean);
    state.tasteDraft.avoids = state.tasteDraft.avoids.filter((item) => item.toLowerCase() !== clean.toLowerCase());
    markTasteDraftChanged();
  }

  function addDraftAvoid(value) {
    const clean = String(value || '').trim().replace(/,$/, '');
    if (!state.tasteDraft) state.tasteDraft = cloneProfile(state.profile);
    if (!clean || state.tasteDraft.avoids.some((item) => item.toLowerCase() === clean.toLowerCase()) || state.tasteDraft.avoids.length >= 12) return;
    state.tasteDraft.avoids.push(clean);
    state.tasteDraft.loves = state.tasteDraft.loves.filter((item) => item.toLowerCase() !== clean.toLowerCase());
    markTasteDraftChanged();
  }

  function saveLastResult(data) {
    state.last = data;
    persistAppState();
  }

  function saveRecent(request, result) {
    const item = {
      id: `${Date.now()}`,
      intent: request.intent,
      prompt: request.goal || intentLabel(request.intent),
      createdAt: new Date().toISOString(),
      request: structuredCloneSafe(request),
      data: result,
    };
    state.recent = [item, ...state.recent.filter((entry) => entry.prompt !== item.prompt)].slice(0, 8);
    persistAppState();
  }

  function structuredCloneSafe(value) {
    try { return JSON.parse(JSON.stringify(value)); } catch { return value; }
  }

  function intentLabel(intent, personalized = false) {
    if ((intent === 'discover' || intent === 'taste') && personalized) return 'Explore art with my saved taste';
    return {
      discover: 'Explore art',
      find: 'Find art',
      taste: 'Explore art with my saved taste',
      curate: 'Curate a collection',
      buy: 'Buy art',
      learn: 'Learn about art',
    }[intent] || 'New request';
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
    }[char]));
  }

  function escapeAttribute(value) { return escapeHtml(value); }

  function setActiveNav() {
    const page = window.ART_CONCIERGE_PAGE || '';
    $$('[data-page]').forEach((link) => {
      if (link.dataset.page === page) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function applyThemeControls() {
    const theme = state.preferences.theme || 'light';
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    const font = state.preferences.fontScale || 'md';
    document.documentElement.style.setProperty('--scale', ({ sm: .92, md: 1, lg: 1.12 }[font] || 1));
    const select = $('#fontScale');
    if (select) select.value = font;
    const button = $('#themeToggle');
    if (button) button.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  }

  function setupGlobalControls() {
    $('#themeToggle')?.addEventListener('click', () => {
      const next = (document.documentElement.dataset.theme || 'light') === 'dark' ? 'light' : 'dark';
      state.preferences.theme = next;
      persistAppState();
      applyThemeControls();
    });
    $('#fontScale')?.addEventListener('change', (event) => {
      const value = event.target.value;
      state.preferences.fontScale = value;
      persistAppState();
      document.documentElement.style.setProperty('--scale', ({ sm: .92, md: 1, lg: 1.12 }[value] || 1));
    });
  }

  function recentMarkup(item) {
    return `
      <article class="recent-item">
        <div class="recent-copy">
          <div class="recent-title">${escapeHtml(item.prompt)}</div>
          <div class="recent-meta">${escapeHtml(intentLabel(item.intent))} · ${escapeHtml(new Date(item.createdAt || item.savedAt || Date.now()).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))}</div>
        </div>
        <button class="secondary-button recent-open" type="button" data-recent-id="${escapeAttribute(item.id)}">Continue</button>
      </article>`;
  }

  function clearCurrentInspirationImages() {
    clearInspirationObjectUrls();
    state.inspiration.images = [];
    state.inspiration.preparing = false;
    const selectedWrap = $('#selectedInspirationList');
    if (selectedWrap) selectedWrap.innerHTML = '';
    const files = $('#inspirationFiles');
    if (files) files.value = '';
  }

  function renderInspirationUI() {
    const selectedWrap = $('#selectedInspirationList');
    if (!selectedWrap) return;
    clearInspirationObjectUrls();
    const records = state.inspiration.images.slice(0, MAX_REQUEST_IMAGES);
    selectedWrap.innerHTML = records.map((record) => `
      <article class="selected-inspiration-item">
        <img src="${escapeAttribute(imageObjectUrl(record))}" alt="${escapeAttribute(record.name)}" loading="lazy" decoding="async">
        <div class="selected-inspiration-copy"><span>${escapeHtml(record.name)}</span><button class="text-button" type="button" data-remove-selected-image="${escapeAttribute(record.id)}">Remove</button></div>
      </article>`).join('');
    const status = $('#inspirationMessage');
    if (status && !state.inspiration.preparing && records.length === 0) status.textContent = '';
  }

  async function handleInspirationUpload(fileList) {
    const files = [...(fileList || [])];
    if (!files.length) return;
    const status = $('#inspirationMessage');
    const errors = [];
    let addedCount = 0;
    state.inspiration.preparing = true;
    try {
      for (const file of files) {
        if (state.inspiration.images.length >= MAX_REQUEST_IMAGES) {
          errors.push(`Only ${MAX_REQUEST_IMAGES} images can be attached to one search.`);
          break;
        }
        try {
          if (status) status.textContent = `Preparing ${safeImageName(file.name)}…`;
          const compressed = await compressInspirationImage(file);
          const record = {
            id: (window.crypto && typeof window.crypto.randomUUID === 'function') ? window.crypto.randomUUID() : `inspiration-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
            name: safeImageName(file.name),
            mimeType: 'image/jpeg',
            blob: compressed.blob,
            width: compressed.width,
            height: compressed.height,
            bytes: compressed.blob.size,
            notes: '',
          };
          state.inspiration.images.push(record);
          addedCount += 1;
        } catch (error) {
          errors.push(error.message || `${safeImageName(file.name)} could not be prepared.`);
        }
      }
    } finally {
      state.inspiration.preparing = false;
    }
    renderInspirationUI();
    if (status) {
      const successText = addedCount
        ? `${addedCount} image${addedCount === 1 ? '' : 's'} attached to this search only. They will be discarded when this search finishes.`
        : '';
      status.textContent = [successText, ...errors].filter(Boolean).join(' ');
    }
  }

  function purchaseStatusInfo(purchase) {
    const paymentStatus = String(purchase.paymentStatus || '').toLowerCase();
    const checkoutStatus = String(purchase.checkoutStatus || '').toLowerCase();
    if (paymentStatus === 'paid' || paymentStatus === 'no_payment_required') return { key: 'paid', label: 'Payment confirmed' };
    if (checkoutStatus === 'expired') return { key: 'expired', label: 'Checkout expired' };
    if (checkoutStatus === 'complete') return { key: 'processing', label: 'Payment processing' };
    if (checkoutStatus === 'open') return { key: 'awaiting', label: 'Payment not confirmed' };
    return { key: 'unknown', label: 'Status not verified' };
  }

  function formatPurchaseAmount(amountTotal, currency) {
    if (amountTotal === null || amountTotal === undefined || !Number.isFinite(Number(amountTotal))) return 'Not available';
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency', currency: String(currency || 'CAD').toUpperCase(),
      }).format(Number(amountTotal) / 100);
    } catch {
      return `${(Number(amountTotal) / 100).toFixed(2)} ${String(currency || 'CAD').toUpperCase()}`;
    }
  }

  function renderPurchaseHistory() {
    const list = $('#dashboardPurchases');
    if (!list) return;
    const purchases = Array.isArray(state.purchases) ? state.purchases.slice(0, 10) : [];
    if (!purchases.length) {
      list.innerHTML = '';
      return;
    }
    list.innerHTML = purchases.map((purchase) => {
      const status = purchaseStatusInfo(purchase);
      const createdAt = purchase.createdAt ? new Date(purchase.createdAt).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : 'Date not available';
      const reference = String(purchase.sessionId || '');
      const shortReference = reference.length > 19 ? `${reference.slice(0, 9)}…${reference.slice(-7)}` : reference;
      return `<article class="purchase-record">
        <div class="purchase-record-main">
          <strong>${escapeHtml(purchase.title || purchase.artworkId || 'Artwork checkout')}</strong>
          <span>${escapeHtml(purchase.artist || 'Artist not provided')}</span>
          <small>${escapeHtml(createdAt)} · Ref ${escapeHtml(shortReference)}</small>
        </div>
        <div class="purchase-record-summary">
          <span class="purchase-status purchase-status--${status.key}">${escapeHtml(status.label)}</span>
          <strong>${escapeHtml(formatPurchaseAmount(purchase.amountTotal, purchase.currency))}</strong>
          <button class="secondary-button purchase-check-button" type="button" data-purchase-session="${escapeAttribute(reference)}">Check payment</button>
        </div>
      </article>`;
    }).join('');
  }

  function renderDashboard() {
    const recent = $('#dashboardRecent');
    const saved = $('#dashboardSaved');
    const savedEmpty = $('#dashboardSavedEmpty');
    if (!recent && !saved && !$('#dashboardPurchases')) return;

    if (recent) {
      recent.innerHTML = state.recent.length
        ? state.recent.slice(0, 5).map(recentMarkup).join('')
        : '<p class="empty-state">Your recent conversations will appear here after your first search.</p>';
    }

    if (saved) {
      const works = state.saved.slice(0, 3);
      saved.innerHTML = works.map((work) => {
        const id = escapeAttribute(work.id);
        const savedImageUrl = work.display_image_url || work.image_url || artworkPreviewUrl(work.detail_url) || artworkFallbackUrl(work.title, work.artist);
        const image = `<img data-artwork-image data-artwork-title="${escapeAttribute(work.title || '')}" data-artwork-artist="${escapeAttribute(work.artist || '')}" data-detail-url="${escapeAttribute(work.detail_url || '')}" ${work.display_image_url && work.display_image_url !== work.image_url && work.image_url ? `data-original-src="${escapeAttribute(work.image_url)}"` : ''} ${work.detail_url && savedImageUrl === artworkPreviewUrl(work.detail_url) ? 'data-tried-detail-preview="true"' : ''} ${savedImageUrl === artworkFallbackUrl(work.title, work.artist) ? 'data-tried-image-fallback="true"' : ''} src="${escapeAttribute(savedImageUrl)}" alt="${escapeAttribute(work.title || 'Artwork')} by ${escapeAttribute(work.artist || 'Unknown artist')}" loading="eager" decoding="async" fetchpriority="high" referrerpolicy="strict-origin-when-cross-origin">`;
        return `<button class="saved-thumb" type="button" data-saved-id="${id}" aria-label="Open ${escapeAttribute(work.title || 'saved artwork')}">${image}</button>`;
      }).join('');
      bindArtworkImageFallbacks(saved);
      if (savedEmpty) savedEmpty.hidden = works.length > 0;
    }

    renderPurchaseHistory();
    renderTastePage();

    const dashboardTabs = [
      { name: 'overview', tab: $('#overviewTab'), panel: $('#overviewPanel') },
      { name: 'taste', tab: $('#tasteTab'), panel: $('#tastePanel') },
    ].filter((item) => item.tab && item.panel);
    const activateTab = (tabName, moveFocus = false, updateHash = false) => {
      const selected = dashboardTabs.find((item) => item.name === tabName) || dashboardTabs[0];
      dashboardTabs.forEach((item) => {
        const active = item === selected;
        item.tab.classList.toggle('is-active', active);
        item.tab.setAttribute('aria-selected', String(active));
        item.tab.setAttribute('tabindex', active ? '0' : '-1');
        item.panel.hidden = !active;
        item.panel.classList.toggle('is-active', active);
      });
      if (updateHash) {
        const suffix = selected?.name === 'overview' ? '/dashboard' : `/dashboard#${selected?.name}`;
        window.history.replaceState(null, '', suffix);
      }
      if (moveFocus) selected?.tab.focus();
    };
    dashboardTabs.forEach((item, index) => {
      item.tab.addEventListener('click', () => requestTasteNavigation(() => activateTab(item.name, false, true)));
      item.tab.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
          event.preventDefault();
          requestTasteNavigation(() => activateTab(dashboardTabs[(index + 1) % dashboardTabs.length].name, true, true));
        } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
          event.preventDefault();
          requestTasteNavigation(() => activateTab(dashboardTabs[(index - 1 + dashboardTabs.length) % dashboardTabs.length].name, true, true));
        } else if (event.key === 'Home') {
          event.preventDefault();
          requestTasteNavigation(() => activateTab(dashboardTabs[0].name, true, true));
        } else if (event.key === 'End') {
          event.preventDefault();
          requestTasteNavigation(() => activateTab(dashboardTabs[dashboardTabs.length - 1].name, true, true));
        }
      });
    });
    const hash = window.location.hash.toLowerCase();
    activateTab(hash === '#taste' ? 'taste' : 'overview');
  }

  function renderHome() {
    // The landing page is intentionally focused on the product proposition and primary CTA.
  }

  function intentCopy(intent) {
    return {
      discover: ['Explore art', 'Discover surprising connections. Turn on saved-taste personalization to build on your artworks, preferences, and earlier ideas.', 'I love punk music and bold patterns. Find art with a similar energy, but take me somewhere unexpected.'],
      find: ['Find art', 'Search for a particular artist, artwork, style, subject, or visual detail.', 'Find an artist or artwork with this style, colour palette, or subject.'],
      taste: ['Explore art', 'Discover art in the context of your saved artworks, taste profile, and earlier ideas.', 'Use my saved artworks and ideas to help me understand why I am drawn to bold patterns, expressive colour, and unusual textures, then find related art I can buy.'],
      curate: ['Curate a collection', 'Build a coherent collection of purchasable works around a space, palette, mood, or theme.', 'Find three works for a small living room, with warm colours and an experimental feel.'],
      learn: ['Learn about art', 'Generate a learning guide about an artist, artwork, movement, technique, or idea.', 'Explain abstract expressionism in plain language, including key artists, techniques, and how to look at it.'],
    }[intent] || ['Explore art', 'Discover new directions or personalise the search with your saved taste.', 'Tell me what you are looking for.'];
  }

  function setIntent(intent) {
    // Buy remains available through artwork acquisition actions, not as a Concierge
    // service choice. Old links or persisted 'buy' intents should land on Explore.
    const requested = intent === 'taste' ? 'discover' : intent;
    state.intent = ['discover', 'find', 'curate', 'learn'].includes(requested) ? requested : 'discover';
    const [serviceTitle, description, promptPlaceholder] = intentCopy(state.intent);
    const descriptionNode = $('#serviceDescription');
    const input = $('#requestInput');
    const personalization = $('#personalizationControl');
    const personalizationInput = $('#personalizeExplore');
    const askLabel = $('#askConciergeLabel');
    if (descriptionNode) descriptionNode.textContent = description;
    if (input) input.placeholder = `${promptPlaceholder}\n\nAdd as much or as little detail as you like.`;
    if (askLabel) askLabel.textContent = String(serviceTitle).trim().split(/\s+/)[0] || 'Explore';
    if (personalization) personalization.hidden = state.intent !== 'discover';
    if (personalizationInput) personalizationInput.checked = Boolean(state.personalizeExplore);
  }

  function addProfileReference(value) {
    const clean = value.trim().replace(/,$/, '');
    if (!clean || state.profile.loves.includes(clean) || state.profile.loves.length >= 12) return;
    state.profile.loves.push(clean);
    saveProfile();
  }

  function addProfileAvoid(value) {
    const clean = value.trim().replace(/,$/, '');
    if (!clean || state.profile.avoids.includes(clean) || state.profile.avoids.length >= 12) return;
    state.profile.avoids.push(clean);
    state.profile.loves = state.profile.loves.filter((item) => item.toLowerCase() !== clean.toLowerCase());
    saveProfile();
  }

  function learnFromFeedback(feedback, artwork) {
    if (!artwork?.artist) return;
    const artist = artwork.artist.trim();
    if (/^More like/i.test(feedback || '')) {
      if (!state.profile.loves.some((item) => item.toLowerCase() === artist.toLowerCase())) {
        state.profile.loves.push(artist);
      }
      state.profile.avoids = state.profile.avoids.filter((item) => item.toLowerCase() !== artist.toLowerCase());
      state.profile.loves = state.profile.loves.slice(-12);
      saveProfile();
    } else if (/^Not for me/i.test(feedback || '')) {
      addProfileAvoid(artist);
    }
  }

  function parseBudget(text) {
    const match = text.replace(/,/g, '').match(/(?:\$|CAD\s?)?(\d{3,7})/i);
    return match ? Number(match[1]) : null;
  }

  function inferIntent(text) {
    const lower = text.toLowerCase();
    if (/buy|purchase|acquire|for sale|budget|price/.test(lower)) return 'buy';
    if (/curate|collection|set of|several/.test(lower)) return 'curate';
    if (/learn.*taste|develop.*taste|understand.*like|help me.*taste/.test(lower)) return 'discover';
    if (/specific|looking for|need a|find me/.test(lower)) return 'find';
    if (/artist|artwork|movement|history|about|learn|explain|who is|what is/.test(lower)) return 'learn';
    return 'discover';
  }

  function cleanServicePrompt(intent) {
    return ({ taste: 'explore art with my saved taste', discover: 'explore art', curate: 'curate a collection', buy: 'buy art' })[intent] || '';
  }

  function buildRequest(prompt, feedback = null, explicitIntent = null) {
    const inferred = explicitIntent || inferIntent(prompt);
    const promptRequestsPersonalTaste = /develop(?:ing)? my taste|help me .*taste|understand (?:what|why) .* (?:like|drawn to)|why am i drawn|what i am drawn to|build on my (?:saved )?(?:taste|artworks)/i.test(prompt || '');
    const useSavedTaste = inferred === 'taste' || (inferred === 'discover' && (state.personalizeExplore || promptRequestsPersonalTaste));
    const budget = parseBudget(prompt);
    return {
      intent: inferred === 'taste' ? 'discover' : inferred,
      personalize_with_saved_taste: useSavedTaste,
      loves: [...state.profile.loves],
      avoid_references: [...state.profile.avoids],
      additional_interests: [],
      profile_notes: useSavedTaste ? (String(state.profile.notes || '').trim().slice(0, 1000) || null) : null,
      existing_ideas: useSavedTaste
        ? state.recent.map((item) => String(item.prompt || '').trim()).filter((item) => item && item.toLowerCase() !== cleanServicePrompt(inferred)).slice(0, 4)
        : [],
      existing_artworks: useSavedTaste ? state.saved.slice(0, 6).map((work) => ({
        title: String(work.title || '').slice(0, 180),
        artist: String(work.artist || '').slice(0, 140),
        medium: String(work.medium || '').slice(0, 120),
        description: String(work.description || '').slice(0, 300),
        year: String(work.year || '').slice(0, 40),
      })) : [],
      art_interests: [],
      mediums: [],
      budget_min: null,
      budget_max: budget,
      room: null,
      size_preference: null,
      preferred_market: null,
      discovery_level: state.profile.discoveryLevel,
      purchase_required: inferred === 'buy' || useSavedTaste,
      goal: prompt.trim() || null,
      number_of_works: inferred === 'curate' ? 3 : 1,
      feedback,
      exclude_artwork_ids: [],
      inspiration_image_ids: [],
      inspiration_images: [],
    };
  }

  function resultsRunUrl(intent, fromConcierge = false, personalizeWithSavedTaste = state.personalizeExplore) {
    const runId = (window.crypto && typeof window.crypto.randomUUID === 'function')
      ? window.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const params = new URLSearchParams({ intent: intent || 'discover', run: '1', run_id: runId });
    if (fromConcierge) params.set('from', 'concierge');
    if (personalizeWithSavedTaste) params.set('personalize_taste', '1');
    return `/results?${params.toString()}`;
  }

  async function beginRequest(prompt) {
    const clean = String(prompt || '').trim();
    if (!clean && state.inspiration.images.length === 0) {
      const input = $('#requestInput');
      if (input) {
        input.setCustomValidity('Add a short description or at least one inspiration image.');
        input.reportValidity();
        input.focus();
      }
      return;
    }
    $('#requestInput')?.setCustomValidity('');
    const params = new URLSearchParams(window.location.search);
    const inferred = (params.has('intent') || state.personalizeExplore) ? state.intent : inferIntent(clean);
    const request = buildRequest(clean, null, inferred);
    try {
      const selectedImages = state.inspiration.images.slice(0, MAX_REQUEST_IMAGES);
      request.inspiration_images = await Promise.all(selectedImages.map(async (record) => ({
        image_id: record.id,
        name: record.name,
        notes: record.notes || '',
        mime_type: record.mimeType || 'image/jpeg',
        data: await blobToBase64(record.blob),
      })));
      request.inspiration_image_ids = request.inspiration_images.map((image) => image.image_id);
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(request));
      // Images live only long enough to cross the same-tab page navigation.
      clearCurrentInspirationImages();
      window.location.assign(resultsRunUrl(request.intent, true, request.personalize_with_saved_taste));
    } catch (error) {
      const status = $('#inspirationMessage');
      if (status) status.textContent = error?.name === 'QuotaExceededError'
        ? 'The selected images are too large for this browser tab to pass to the search. Remove one or choose smaller images.'
        : (error.message || 'ArtCo could not prepare the request. Please try again.');
    }
  }

  function beginNotForMeSearch(artwork, button = null) {
    if (!artwork) return;
    const title = String(artwork.title || 'this artwork').trim();
    const artist = String(artwork.artist || '').trim();
    const knownArtist = artist && !/^(unknown|anonymous|various) artist$/i.test(artist);
    const prompt = knownArtist
      ? `Find alternatives to “${title}” with a different visual direction. Do not recommend work by ${artist}.`
      : `Find alternatives to “${title}” with a different visual direction. Do not return this artwork.`;
    const request = buildRequest(prompt, `Not for me: ${knownArtist ? artist : title}`, 'discover');
    request.exclude_artwork_ids = [String(artwork.id || '')].filter(Boolean).slice(0, 20);
    request.avoid_references = [...new Set([
      ...state.profile.avoids,
      ...(knownArtist ? [artist] : []),
    ])].slice(-12);
    request.art_interests = [];
    request.number_of_works = 3;
    request.purchase_required = false;
    request.inspiration_image_ids = [];
    request.inspiration_images = [];
    if (button) { button.disabled = true; button.textContent = 'Searching alternatives…'; }
    try {
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(request));
      window.location.assign(resultsRunUrl('discover'));
    } catch (error) {
      if (button) { button.disabled = false; button.textContent = 'Not for me'; }
      const message = $('#resultsSummary');
      if (message) message.textContent = 'Could not start a new search. Please try again.';
    }
  }

  function beginSimilarSearch(artwork) {
    if (!artwork) return;
    const title = String(artwork.title || 'this artwork').trim();
    const artist = String(artwork.artist || '').trim();
    const subject = [title, artist && `by ${artist}`].filter(Boolean).join(' ');
    const prompt = `Find artworks similar to ${subject}. Prioritise related subject matter, visual style, medium, composition, and period. Do not return the exact same work.`;
    const request = buildRequest(prompt, `More like ${artist || title}`, 'discover');
    request.art_interests = [...new Set([title, artist, artwork.medium].filter((value) => typeof value === 'string' && value.trim()))].slice(0, 8);
    request.number_of_works = 3;
    request.purchase_required = false;
    // This action is anchored to the saved artwork, not any previously selected
    // inspiration image that may still be in the one-tab request state.
    request.inspiration_image_ids = [];
    request.inspiration_images = [];
    try {
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(request));
    } catch (error) {
      const count = $('#savedCount');
      if (count) count.textContent = 'Could not start the search in this browser tab. Please allow session storage and try again.';
      return;
    }
    window.location.assign(resultsRunUrl('discover'));
  }

  async function runPendingRequest() {
    const resultsPage = $('#resultsPage');
    if (!resultsPage) return false;
    const raw = sessionStorage.getItem('artConciergePendingRequest');
    if (!raw) {
      // Never fall back to a cached previous search on a URL that explicitly
      // represents a new run. The request handoff may have been cleared by a
      // browser restore or storage failure; show a clean recoverable state.
      const heading = $('#resultsHeading');
      const summary = $('#resultsSummary');
      const grid = $('#artGrid');
      if (heading) heading.textContent = 'This search could not be started.';
      if (summary) summary.textContent = 'Return to your concierge and submit the request again.';
      if (grid) grid.innerHTML = '<p class="empty-state">No current request was received.</p>';
      return false;
    }
    let payload;
    try { payload = JSON.parse(raw); } catch {
      sessionStorage.removeItem('artConciergePendingRequest');
      const heading = $('#resultsHeading');
      const summary = $('#resultsSummary');
      const grid = $('#artGrid');
      if (heading) heading.textContent = 'This search could not be started.';
      if (summary) summary.textContent = 'The request could not be read. Return to your concierge and try again.';
      if (grid) grid.innerHTML = '<p class="empty-state">No current request was received.</p>';
      return false;
    }
    // Consume the one-time handoff before the API call. The image bytes are not
    // retained in sessionStorage, the current result object, or recent-search history.
    sessionStorage.removeItem('artConciergePendingRequest');

    // Invalidate the previously persisted result for this page instance before
    // any asynchronous request starts. This prevents delayed UI events, BFCache
    // restores, or save actions from repainting the prior search mid-transition.
    state.searchRunActive = true;
    state.last = null;
    state.intent = payload.intent || state.intent;
    state.personalizeExplore = Boolean(payload.personalize_with_saved_taste);
    const resultsHeading = $('#resultsHeading');
    const resultsSummary = $('#resultsSummary');
    const resultsMeta = $('#resultsMeta');
    const grid = $('#artGrid');
    const notForSaleSection = $('#notForSaleSection');
    const notForSaleGrid = $('#notForSaleGrid');
    const recommendationSection = $('#recommendationsSection');
    const refinePanel = $('#refinePanel');
    const tasteSummary = $('#tasteSummary');
    const visualSummary = $('#visualInspirationSummary');
    const learningGuide = $('#learningGuideSection');
    const refineButton = $('#refineButton');
    if (resultsHeading) resultsHeading.textContent = 'I’m researching that now.';
    if (resultsSummary) resultsSummary.textContent = 'Starting a fresh search for your current request.';
    if (resultsMeta) { resultsMeta.textContent = ''; resultsMeta.hidden = true; }
    if (grid) grid.innerHTML = '<p class="empty-state">Searching and narrowing the field…</p>';
    if (notForSaleSection) notForSaleSection.hidden = true;
    if (notForSaleGrid) notForSaleGrid.innerHTML = '';
    if (tasteSummary) tasteSummary.hidden = true;
    if (visualSummary) { visualSummary.hidden = true; visualSummary.innerHTML = ''; }
    if (learningGuide) learningGuide.hidden = true;
    if (recommendationSection) recommendationSection.hidden = state.intent === 'learn';
    if (refinePanel) refinePanel.hidden = state.intent === 'learn';
    if (refineButton) { refineButton.disabled = true; refineButton.textContent = 'Researching…'; }

    let apiPayload = null;
    try {
      apiPayload = {
        ...payload,
        inspiration_images: Array.isArray(payload.inspiration_images) ? payload.inspiration_images.slice(0, MAX_REQUEST_IMAGES) : [],
        inspiration_image_ids: Array.isArray(payload.inspiration_images) ? payload.inspiration_images.slice(0, MAX_REQUEST_IMAGES).map((image) => image.image_id) : [],
      };
      const response = await fetch('/api/concierge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(apiPayload),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail?.[0]?.msg || detail?.detail || `Request failed (${response.status})`);
      }
      const data = await response.json();
      if (data.success === false) throw new Error(data.detail || 'The image-informed search could not be completed.');
      const historyRequest = { ...payload };
      delete historyRequest.inspiration_images;
      delete historyRequest.inspiration_image_ids;
      data.request = historyRequest;
      state.searchRunActive = false;
      saveLastResult(data);
      saveRecent(historyRequest, data);
      renderResults(data);
      return true;
    } catch (error) {
      state.searchRunActive = false;
      state.last = null;
      if (resultsHeading) resultsHeading.textContent = 'I couldn’t complete that search.';
      if (resultsSummary) resultsSummary.textContent = error.message || 'Please try again.';
      if (resultsMeta) { resultsMeta.textContent = ''; resultsMeta.hidden = true; }
      if (grid) grid.innerHTML = '<p class="empty-state">Return to your concierge and try the request again.</p>';
      if (refineButton) { refineButton.disabled = true; refineButton.textContent = 'Refine'; }
      return false;
    } finally {
      state.searchRunActive = false;
      if (refineButton) { refineButton.disabled = false; refineButton.textContent = 'Refine'; }
      if (apiPayload) apiPayload.inspiration_images = [];
      if (payload && typeof payload === 'object') {
        delete payload.inspiration_images;
        delete payload.inspiration_image_ids;
      }
      apiPayload = null;
    }
  }

  function renderTasteSummary(data) {
    const box = $('#tasteSummary');
    if (!box) return;
    const inputs = data.taste?.inputs || [];
    const visual = data.visual_inspiration?.visual_attributes || [];
    const colours = data.visual_inspiration?.colour_palette || [];
    const tags = [...new Set([...inputs, ...visual, ...colours].filter(Boolean))].slice(0, 8);
    if (!tags.length) { box.hidden = true; return; }
    box.hidden = false;
    const hasCulturalInputs = inputs.length > 0;
    const hasVisualInputs = visual.length > 0 || colours.length > 0;
    $('#tasteSummaryCopy').textContent = hasCulturalInputs && hasVisualInputs
      ? 'Your stated interests and visual references guided this search.'
      : hasVisualInputs
        ? 'The visual traits in your inspiration images guided this search.'
        : 'Your stated interests guided this search.';
    $('#tasteTags').innerHTML = tags.map((tag) => `<span class="taste-tag">${escapeHtml(tag)}</span>`).join('');
  }

  function artworkFallbackUrl(title, artist) {
    const params = new URLSearchParams({ title: String(title || ''), artist: String(artist || '') });
    return `/api/artwork-image-fallback?${params.toString()}`;
  }

  function bindArtworkImageFallbacks(root) {
    if (!root) return;
    root.querySelectorAll('img[data-artwork-image]').forEach((image) => {
      if (image.dataset.fallbackBound === 'true') return;
      image.dataset.fallbackBound = 'true';
      const advanceFallback = () => {
        // The source page may publish a working image even when its raw API
        // thumbnail blocks hotlinking. Try the same-origin page-preview proxy
        // first, then fall back to the direct image URL.
        const detailUrl = image.dataset.detailUrl;
        if (detailUrl && image.dataset.triedDetailPreview !== 'true') {
          image.dataset.triedDetailPreview = 'true';
          image.src = `/api/artwork-preview?url=${encodeURIComponent(detailUrl)}`;
          return;
        }
        const originalSrc = image.dataset.originalSrc;
        if (originalSrc && image.dataset.triedOriginalSrc !== 'true') {
          image.dataset.triedOriginalSrc = 'true';
          image.src = originalSrc;
          return;
        }
        if (image.dataset.triedImageFallback !== 'true') {
          image.dataset.triedImageFallback = 'true';
          image.src = artworkFallbackUrl(image.dataset.artworkTitle, image.dataset.artworkArtist);
          return;
        }
        // Do not leave a visible recommendation without an image. Remove a card
        // only after both source-page recovery and exact-work museum fallback fail.
        const card = image.closest('.art-card');
        const savedThumb = image.closest('.saved-thumb');
        if (card) card.remove();
        else if (savedThumb) savedThumb.remove();
      };
      image.addEventListener('error', advanceFallback);
      // A cached request can fail before handlers are attached after the card
      // markup is inserted. Check that case as well, not only future error events.
      if (image.complete && image.naturalWidth === 0) advanceFallback();
    });
  }

  function artworkPreviewUrl(detailUrl) {
    return detailUrl ? `/api/artwork-preview?url=${encodeURIComponent(detailUrl)}` : '';
  }

  function renderCard(work) {
    const available = work.availability === 'available' && work.source_kind === 'commercial';
    const availability = available ? 'available' : 'not_for_sale';
    const meta = [work.medium, work.dimensions, work.year, work.price_label].filter(Boolean).join(' · ');
    const directImageUrl = work.image_url || '';
    const pagePreviewUrl = artworkPreviewUrl(work.detail_url);
    const imageDisplayUrl = work.display_image_url || directImageUrl || pagePreviewUrl || artworkFallbackUrl(work.title, work.artist);
    const previewIsInitial = Boolean(pagePreviewUrl && imageDisplayUrl === pagePreviewUrl);
    const fallbackIsInitial = imageDisplayUrl === artworkFallbackUrl(work.title, work.artist);
    const image = `<img data-artwork-image data-artwork-title="${escapeAttribute(work.title || '')}" data-artwork-artist="${escapeAttribute(work.artist || '')}" data-detail-url="${escapeAttribute(work.detail_url || '')}" ${work.display_image_url && work.display_image_url !== directImageUrl && directImageUrl ? `data-original-src="${escapeAttribute(directImageUrl)}"` : ''} ${previewIsInitial ? 'data-tried-detail-preview="true"' : ''} ${fallbackIsInitial ? 'data-tried-image-fallback="true"' : ''} src="${escapeAttribute(imageDisplayUrl)}" alt="${escapeAttribute(work.title || 'Artwork')} by ${escapeAttribute(work.artist || 'Unknown artist')}" loading="eager" decoding="async" fetchpriority="high" referrerpolicy="strict-origin-when-cross-origin">`;
    const id = escapeAttribute(work.id);
    const linkLabel = available ? 'Buy / enquire' : 'See at source';
    const externalLink = work.detail_url
      ? `<a class="card-action" href="${escapeAttribute(work.detail_url)}" target="_blank" rel="noopener noreferrer">${linkLabel} →</a>`
      : '';
    const checkoutButton = available && work.checkout_available
      ? `<button class="card-action buy-action" type="button" data-id="${id}">Buy securely</button>`
      : '';
    const link = available && work.checkout_available ? `${checkoutButton}${externalLink}` : externalLink;
    const saved = state.saved.some((item) => item.id === work.id);
    const reasons = (work.why || []).map((item) => String(item)).join(' · ');
    return `
      <article class="art-card" data-art-id="${id}">
        <div class="art-image-wrap">${image}</div>
        <div class="art-body">
          <div class="art-title">${escapeHtml(work.title || 'Untitled')}</div>
          <div class="art-artist">${escapeHtml(work.artist || 'Unknown artist')}</div>
          <div class="art-meta"${meta ? '' : ' hidden'}>${escapeHtml(meta)}</div>
          <div class="art-reason">${escapeHtml(reasons)}</div>
          <div class="art-footer"><span class="availability ${availability}">${available ? 'AVAILABLE TO ACQUIRE' : 'NOT FOR SALE'}</span><span class="art-source" title="${escapeAttribute(work.source || '')}">${escapeHtml(work.source || '')}</span></div>
          <div class="art-actions">
            ${link}
            <button class="card-action save-action" type="button" data-id="${id}" aria-pressed="${saved}">${saved ? 'Saved' : 'Save'}</button>
            <button class="card-action feedback-action" type="button" data-id="${id}" data-feedback="More like ${escapeAttribute(work.artist || work.title)}">More like this</button>
            <button class="card-action feedback-action" type="button" data-id="${id}" data-feedback="Not for me: ${escapeAttribute(work.artist || work.title)}">Not for me</button>
          </div>
        </div>
      </article>`;
  }

  function renderLearningGuide(guide, intent) {
    const section = $('#learningGuideSection');
    if (!section) return;
    if (intent !== 'learn' || !guide || !guide.overview) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    $('#learningGuideTopic').textContent = guide.topic ? `A starting point: ${guide.topic}` : 'A starting point for exploring the subject';
    $('#learningGuideOverview').textContent = guide.overview;
    const list = (id, values) => {
      const node = $(`#${id}`);
      if (node) node.innerHTML = (values || []).map((value) => `<li>${escapeHtml(value)}</li>`).join('');
    };
    list('learningKeyIdeas', guide.key_ideas);
    list('learningLookCloser', guide.look_closer);
    list('learningQuestions', guide.questions);
    const terms = $('#learningTerms');
    if (terms) terms.innerHTML = (guide.terms || []).map((item) => `<div class="learning-term"><dt>${escapeHtml(item.term || '')}</dt><dd>${escapeHtml(item.definition || '')}</dd></div>`).join('');
    const examplesSection = $('#learningExamplesSection');
    const examples = $('#learningExamples');
    const works = Array.isArray(guide.related_works) ? guide.related_works : [];
    if (examplesSection && examples) {
      examplesSection.hidden = works.length === 0;
      examples.innerHTML = works.map((work) => {
        const details = [work.medium, work.year, work.source].filter(Boolean).join(' · ');
        const link = work.detail_url ? `<a href="${escapeAttribute(work.detail_url)}" target="_blank" rel="noopener noreferrer">View source record →</a>` : '';
        return `<article class="learning-example"><strong>${escapeHtml(work.title || 'Untitled')}</strong><span>${escapeHtml(work.artist || 'Unknown artist')}</span><small>${escapeHtml(details)}</small>${link}</article>`;
      }).join('');
    }
    const note = $('#learningSourceNote');
    if (note) note.textContent = guide.source_note || '';
  }

  function renderResults(data) {
    // Ignore stale render attempts while a fresh request owns the Results view.
    if (state.searchRunActive) return;
    const intent = data.intent || state.intent;
    const labels = {
      discover: data.request?.personalize_with_saved_taste ? 'Art to explore from your taste.' : 'Works worth a closer look.',
      find: 'The closest fits I found.',
      taste: 'Art to explore from your taste.',
      curate: 'Works that make sense together.',
      buy: 'Art you can act on now.',
      learn: 'Your learning guide.',
    };
    $('#resultsHeading').textContent = labels[intent] || labels.discover;
    $('#resultsSummary').textContent = data.summary || '';
    const learningOnly = intent === 'learn';
    const recommendationSection = $('#recommendationsSection');
    const refinePanel = $('#refinePanel');
    if (recommendationSection) recommendationSection.hidden = learningOnly;
    if (refinePanel) refinePanel.hidden = learningOnly;
    const resultsMeta = $('#resultsMeta');
    if (resultsMeta) resultsMeta.hidden = learningOnly;
    // Curate, personalised Explore, and Buy only show commercial inventory explicitly
    // listed as available. This guard also protects against older cached responses.
    const usesSavedTaste = intent === 'taste' || Boolean(data.request?.personalize_with_saved_taste);
    const purchaseOnlyIntent = ['buy', 'curate', 'taste'].includes(intent) || usesSavedTaste;
    const results = (data.results || []).filter((work) => !purchaseOnlyIntent
      || (work.source_kind === 'commercial' && work.availability === 'available')).slice(0, 3);
    const notForSale = [];
    const visibleCount = results.length + notForSale.length;
    const visiblePurchasable = results.filter((work) => work.availability === 'available' && work.source_kind === 'commercial').length;
    $('#resultsMeta').textContent = purchaseOnlyIntent
      ? `${visibleCount} work${visibleCount === 1 ? '' : 's'} shown · all available to acquire`
      : `${visibleCount} recommendation${visibleCount === 1 ? '' : 's'} shown`;
    if (results.length) {
      $('#artGrid').innerHTML = results.map(renderCard).join('');
      bindArtworkImageFallbacks($('#artGrid'));
    } else {
      const configuredSellers = data.diagnostics?.commercial_sources_configured || [];
      const emptyMessage = purchaseOnlyIntent
        ? (configuredSellers.length
          ? 'No works explicitly listed as available to purchase matched this request. Try a different artist, style, medium, location, or budget.'
          : 'No commercial seller inventory is connected right now. These recommendations only show works listed as available to purchase; museum collection records are not substituted.')
        : 'The connected art collections did not return a match with a retrievable image. Try a broader art style, artist, subject, or medium.';
      // Provider configuration and HTTP failures stay in /api/diagnostics and
      // server logs. They are not user-facing copy and must never leak a museum
      // outage into the Buy experience.
      $('#artGrid').innerHTML = `<p class="empty-state">${escapeHtml(emptyMessage)}</p>`;
    }

    $('#notForSaleSection').hidden = purchaseOnlyIntent || !notForSale.length;
    if (notForSale.length) {
      $('#notForSaleGrid').innerHTML = notForSale.map(renderCard).join('');
      bindArtworkImageFallbacks($('#notForSaleGrid'));
      $('#notForSaleHint').textContent = 'Additional works from museum and institutional collections. These are not available for purchase.';
    }
    renderLearningGuide(data.learning_guide, intent);
    renderVisualInspirationSummary(data.visual_inspiration || {}, intent);
    renderTasteSummary(data);
    if (learningOnly) {
      const tasteSummary = $('#tasteSummary');
      if (tasteSummary) tasteSummary.hidden = true;
    }
  }

  function renderVisualInspirationSummary(inspiration, intent = state.intent) {
    const panel = $('#visualInspirationSummary');
    if (!panel) return;
    const count = Number(inspiration.image_count || 0);
    if (!count) { panel.hidden = true; panel.innerHTML = ''; return; }
    panel.hidden = false;
    const heading = intent === 'learn' ? 'How your image informed the guide' : inspiration.analyzed ? 'How your images guided the search' : 'Your images were received';
    const detail = inspiration.analyzed
      ? (inspiration.summary || (intent === 'learn' ? 'ArtCo used the visual characteristics in your image as part of this learning guide.' : 'ArtCo used the visual characteristics in your images to guide the artwork search.'))
      : (inspiration.message || 'Visual analysis was unavailable, so this search used your written request and saved taste.');
    const tags = [...new Set([...(inspiration.visual_attributes || []), ...(inspiration.colour_palette || [])])].slice(0, 8);
    panel.innerHTML = `<div class="visual-inspiration-result-copy"><strong>${escapeHtml(heading)}</strong><p>${escapeHtml(detail)}</p></div>${tags.length ? `<div class="visual-inspiration-result-tags">${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join('')}</div>` : ''}`;
  }

  function getArtworkById(id) {
    return state.last?.results?.find((work) => work.id === id)
      || state.last?.not_for_sale?.find((work) => work.id === id)
      || state.saved.find((work) => work.id === id)
      || null;
  }

  function toggleSave(id) {
    const artwork = getArtworkById(id);
    if (!artwork) return;
    const index = state.saved.findIndex((item) => item.id === id);
    if (index >= 0) state.saved.splice(index, 1);
    else state.saved.unshift(artwork);
    state.saved = state.saved.slice(0, 40);
    persistAppState();
    if (window.ART_CONCIERGE_PAGE === 'results' && state.last) renderResults(state.last);
    if (window.ART_CONCIERGE_PAGE === 'saved') renderSavedPage();
  }

  function renderSavedPage() {
    const grid = $('#savedGrid');
    if (!grid) return;
    const count = $('#savedCount');
    if (count) count.textContent = state.saved.length ? `${state.saved.length} saved work${state.saved.length === 1 ? '' : 's'}` : '';
    if (!state.saved.length) {
      grid.innerHTML = '<p class="empty-state">You have not saved any works yet. Start a conversation with your concierge to find something worth keeping.</p>';
      return;
    }
    grid.innerHTML = state.saved.map(renderCard).join('');
    bindArtworkImageFallbacks(grid);
  }

  function renderTastePage() {
    if (!state.tasteDraft) state.tasteDraft = cloneProfile(state.profile);
    const profile = state.tasteDraft;
    const chips = $('#tasteChips');
    const avoids = $('#avoidChips');
    const notes = $('#tasteNotes');
    const slider = $('#discoveryLevel');
    const updated = $('#tasteUpdated');
    if (!chips || !notes) return;
    chips.innerHTML = profile.loves.map((value, index) => `
      <span class="chip">${escapeHtml(value)} <button type="button" data-remove-index="${index}" aria-label="Remove ${escapeAttribute(value)} from things you love">×</button></span>
    `).join('');
    if (avoids) avoids.innerHTML = profile.avoids.map((value, index) => `
      <span class="chip">${escapeHtml(value)} <button type="button" data-remove-list="avoids" data-remove-index="${index}" aria-label="Remove ${escapeAttribute(value)} from things you avoid">×</button></span>
    `).join('');
    notes.value = profile.notes;
    if (slider) slider.value = String(profile.discoveryLevel);
    if (updated) updated.textContent = tasteDraftIsDirty() ? 'Unsaved changes' : (state.profile.updatedAt ? `Updated ${new Date(state.profile.updatedAt).toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' })}` : 'Not yet saved');
  }

  async function recordCheckoutStarted(session, artwork) {
    const now = new Date().toISOString();
    const sessionId = String(session.session_id || '').trim();
    if (!sessionId) throw new Error('The checkout session was created without a reference. Please try again.');
    const record = {
      sessionId,
      artworkId: String(artwork?.id || session.artwork_id || ''),
      title: String(artwork?.title || 'Artwork checkout'),
      artist: String(artwork?.artist || 'Artist not provided'),
      imageUrl: String(artwork?.display_image_url || artwork?.image_url || ''),
      checkoutStatus: 'open',
      paymentStatus: 'unpaid',
      amountTotal: null,
      currency: null,
      createdAt: now,
      updatedAt: now,
    };
    state.purchases = [record, ...state.purchases.filter((item) => item.sessionId !== sessionId)].slice(0, 12);
    await persistAppState();
    return record;
  }

  async function recordVerifiedPurchase(data) {
    const sessionId = String(data.session_id || '').trim();
    if (!sessionId) return null;
    const existing = state.purchases.find((item) => item.sessionId === sessionId) || {};
    const paymentStatus = String(data.payment_status || existing.paymentStatus || 'unpaid').toLowerCase();
    const checkoutStatus = String(data.status || existing.checkoutStatus || 'open').toLowerCase();
    const artworkId = String(data.artwork_id || existing.artworkId || '');
    const record = {
      ...existing,
      sessionId,
      artworkId,
      title: existing.title || artworkId || 'Artwork checkout',
      artist: existing.artist || 'Artist not provided',
      checkoutStatus,
      paymentStatus,
      amountTotal: data.amount_total ?? existing.amountTotal ?? null,
      currency: data.currency || existing.currency || null,
      createdAt: existing.createdAt || (data.created ? new Date(Number(data.created) * 1000).toISOString() : new Date().toISOString()),
      updatedAt: new Date().toISOString(),
    };
    state.purchases = [record, ...state.purchases.filter((item) => item.sessionId !== sessionId)].slice(0, 12);
    await persistAppState();
    return record;
  }

  async function checkTrackedPurchase(sessionId, button) {
    const message = $('#dashboardPurchasesMessage');
    const session = String(sessionId || '').trim();
    if (!session) return;
    if (button) { button.disabled = true; button.textContent = 'Checking…'; }
    if (message) message.textContent = 'Checking payment status with Stripe…';
    try {
      const response = await fetch(`/api/checkout/status?session_id=${encodeURIComponent(session)}`, { headers: { Accept: 'application/json' } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) throw new Error(data.detail || 'Payment status could not be checked.');
      await recordVerifiedPurchase(data);
      renderPurchaseHistory();
      if (message) message.textContent = 'Payment status updated from Stripe.';
    } catch (error) {
      if (message) message.textContent = error.message || 'Payment status could not be checked. Try again.';
      if (button) { button.disabled = false; button.textContent = 'Check payment'; }
    }
  }

  async function startCheckout(id, button) {
    const message = $('#purchaseMessage');
    const artwork = getArtworkById(String(id));
    if (button) {
      button.disabled = true;
      button.textContent = 'Opening checkout…';
    }
    if (message) message.textContent = 'Opening secure checkout…';
    try {
      const response = await fetch('/api/checkout/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artwork_id: id }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success || !data.checkout_url || !data.session_id) {
        throw new Error(data.detail || 'Checkout is not available for this work.');
      }
      await recordCheckoutStarted(data, artwork);
      window.location.assign(data.checkout_url);
    } catch (error) {
      if (message) message.textContent = error.message || 'Checkout could not be started.';
      if (button) {
        button.disabled = false;
        button.textContent = 'Buy securely';
      }
    }
  }

  async function refreshCheckoutStatus() {
    const card = $('#checkoutStatusCard');
    if (!card) return;
    const heading = $('#checkoutStatusHeading');
    const message = $('#checkoutStatusMessage');
    const details = $('#checkoutOrderDetails');
    const reference = $('#checkoutStatusReference');
    const sessionId = card.dataset.sessionId || new URLSearchParams(window.location.search).get('session_id') || '';
    if (!sessionId) {
      if (heading) heading.textContent = 'Payment status unavailable';
      if (message) message.textContent = 'This return link does not include a Stripe Checkout Session ID, so ArtCo cannot verify payment.';
      return;
    }
    const retry = $('#refreshCheckoutStatus');
    if (retry) { retry.disabled = true; retry.textContent = 'Checking…'; }
    try {
      const response = await fetch(`/api/checkout/status?session_id=${encodeURIComponent(sessionId)}`, { headers: { Accept: 'application/json' } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) throw new Error(data.detail || 'Stripe could not verify this checkout.');
      const payment = String(data.payment_status || 'unpaid').toLowerCase();
      const status = String(data.status || 'open').toLowerCase();
      await recordVerifiedPurchase(data);
      if (payment === 'paid' || payment === 'no_payment_required') {
        if (heading) heading.textContent = 'Payment confirmed';
        if (message) message.textContent = 'Stripe confirms this checkout is paid. Keep the checkout reference for any questions about the purchase.';
      } else if (status === 'complete') {
        if (heading) heading.textContent = 'Payment processing';
        if (message) message.textContent = 'Stripe reports that checkout finished, but payment is not yet marked as paid. Check again shortly before treating this order as confirmed.';
      } else if (status === 'expired') {
        if (heading) heading.textContent = 'Checkout expired';
        if (message) message.textContent = 'Stripe reports that this checkout session expired before payment was confirmed.';
      } else {
        if (heading) heading.textContent = 'Payment not confirmed';
        if (message) message.textContent = 'Stripe has not marked this checkout as paid. You can return to the recommendations and start checkout again.';
      }
      if (details) {
        const amount = Number.isFinite(Number(data.amount_total)) && data.amount_total !== null
          ? new Intl.NumberFormat(undefined, { style: 'currency', currency: String(data.currency || 'CAD').toUpperCase() }).format(Number(data.amount_total) / 100)
          : 'Not available';
        const rows = [
          ['Payment', payment === 'paid' || payment === 'no_payment_required' ? 'Paid' : payment === 'unpaid' ? 'Not paid' : payment],
          ['Artwork reference', data.artwork_id || 'Not provided'],
          ['Total', amount],
          ['Checkout state', status],
        ];
        details.innerHTML = rows.map(([label, value]) => `<div class="checkout-order-row"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('');
        details.hidden = false;
      }
      if (reference) reference.textContent = `Checkout reference: ${data.session_id || sessionId}`;
      card.dataset.checkoutStatus = payment === 'paid' || payment === 'no_payment_required' ? 'paid' : status;
    } catch (error) {
      if (heading) heading.textContent = 'Unable to verify payment';
      if (message) message.textContent = error.message || 'Stripe could not verify this checkout. Try checking again.';
      if (reference) reference.textContent = `Checkout reference: ${sessionId}`;
    } finally {
      if (retry) { retry.disabled = false; retry.textContent = 'Check status again'; }
    }
  }

  function initCheckoutStatus() {
    $('#refreshCheckoutStatus')?.addEventListener('click', refreshCheckoutStatus);
    refreshCheckoutStatus();
  }

  function handleGlobalClicks() {
    document.addEventListener('click', (event) => {
      const purchaseCheck = event.target.closest('[data-purchase-session]');
      if (purchaseCheck) { checkTrackedPurchase(purchaseCheck.dataset.purchaseSession, purchaseCheck); return; }

      const removeSelectedImage = event.target.closest('[data-remove-selected-image]');
      if (removeSelectedImage) {
        state.inspiration.images = state.inspiration.images.filter((image) => image.id !== removeSelectedImage.dataset.removeSelectedImage);
        renderInspirationUI();
        const status = $('#inspirationMessage');
        if (status) status.textContent = state.inspiration.images.length
          ? `${state.inspiration.images.length} image${state.inspiration.images.length === 1 ? '' : 's'} attached to this search only.`
          : 'No inspiration images attached. Add images for this search only.';
        return;
      }

      const save = event.target.closest('.save-action');
      if (save) { toggleSave(save.dataset.id); return; }

      const buy = event.target.closest('.buy-action');
      if (buy) {
        startCheckout(buy.dataset.id, buy);
        return;
      }

      const feedback = event.target.closest('.feedback-action');
      if (feedback) {
        const artwork = getArtworkById(feedback.dataset.id);
        const text = feedback.dataset.feedback || '';
        learnFromFeedback(text, artwork);
        if (/^Not for me/i.test(text) && artwork) {
          beginNotForMeSearch(artwork, feedback);
          return;
        }
        if (window.ART_CONCIERGE_PAGE === 'saved' && /^More like/i.test(text) && artwork) {
          beginSimilarSearch(artwork);
          return;
        }
        const input = $('#feedback');
        if (input) { input.value = text; input.focus(); }
        return;
      }

      const recent = event.target.closest('.recent-open');
      if (recent) {
        const item = state.recent.find((entry) => entry.id === recent.dataset.recentId);
        if (item?.request) {
          sessionStorage.setItem('artCoResumeRequest', JSON.stringify(item.request));
          window.location.href = `/concierge?intent=${encodeURIComponent(item.intent)}&resume=1`;
        } else if (item?.data) {
          saveLastResult(item.data);
          window.location.href = `/results?intent=${encodeURIComponent(item.intent)}`;
        }
        return;
      }

      const savedThumb = event.target.closest('.saved-thumb');
      if (savedThumb) {
        const work = state.saved.find((item) => item.id === savedThumb.dataset.savedId);
        if (work) {
          const data = { intent: 'find', summary: 'Saved from an earlier conversation with your concierge.', results: [work], not_for_sale: work.availability === 'available' ? [] : [work], purchase_available: work.availability === 'available' ? 1 : 0, total_found: 1, taste: { inputs: state.profile.loves } };
          saveLastResult(data);
          window.location.href = '/results?intent=find';
        }
      }

      const remove = event.target.closest('[data-remove-index]');
      if (remove) {
        const tastePanel = $('#tastePanel');
        if (tastePanel && !tastePanel.hidden) {
          if (!state.tasteDraft) state.tasteDraft = cloneProfile(state.profile);
          const list = remove.dataset.removeList === 'avoids' ? state.tasteDraft.avoids : state.tasteDraft.loves;
          list.splice(Number(remove.dataset.removeIndex), 1);
          renderTastePage();
          markTasteDraftChanged();
        } else {
          const list = remove.dataset.removeList === 'avoids' ? state.profile.avoids : state.profile.loves;
          list.splice(Number(remove.dataset.removeIndex), 1);
          saveProfile();
          renderTastePage();
        }
      }
    });
  }

  function initHome() { renderHome(); }
  function initDashboard() {
    renderDashboard();
    initTasteEditor();
    installTasteNavigationGuard();
  }

  async function initConcierge() {
    const params = new URLSearchParams(window.location.search);
    clearCurrentInspirationImages();
    setIntent(state.intent);
    const serviceSelect = $('#serviceSelect');
    if (serviceSelect) serviceSelect.value = state.intent;
    renderInspirationUI();
    $('#inspirationFiles')?.addEventListener('change', (event) => {
      handleInspirationUpload(event.target.files);
      event.target.value = '';
    });

    const resume = params.get('resume');
    if (resume === '1') {
      try {
        const request = JSON.parse(sessionStorage.getItem('artCoResumeRequest') || 'null');
        if (request?.goal) {
          const input = $('#requestInput');
          if (input) input.value = request.goal;
        }
    if (request?.goal) {
          const history = $('#conversationHistory');
          if (history) {
            history.innerHTML = `<div class="conversation-entry"><p class="label">Previous request</p><p>${escapeHtml(request.goal)}</p></div><div class="conversation-entry"><p class="label">Continue from here</p><p>Refine the request, add a preference, or ask me to take it in a new direction.</p></div>`;
          }
        }
        sessionStorage.removeItem('artCoResumeRequest');
      } catch {}
    }

    serviceSelect?.addEventListener('change', (event) => {
      const intent = event.target.value || 'discover';
      state.intent = intent;
      setIntent(intent);
      const url = new URL(window.location.href);
      url.searchParams.set('intent', intent);
      if (state.personalizeExplore && intent === 'discover') url.searchParams.set('personalize_taste', '1');
      else url.searchParams.delete('personalize_taste');
      window.history.replaceState({}, '', url.toString());
    });
    $('#personalizeExplore')?.addEventListener('change', (event) => {
      state.personalizeExplore = Boolean(event.target.checked);
      const url = new URL(window.location.href);
      if (state.personalizeExplore) url.searchParams.set('personalize_taste', '1');
      else url.searchParams.delete('personalize_taste');
      window.history.replaceState({}, '', url.toString());
    });

    const form = $('#requestForm');
    const input = $('#requestInput');
    form?.addEventListener('submit', (event) => {
      event.preventDefault();
      beginRequest(input?.value || '');
    });
    input?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        beginRequest(input.value);
      }
    });
  }
  function initResults() {
    const params = new URLSearchParams(window.location.search);
    const isNewRun = params.get('run') === '1';
    // A new request must never flash or briefly restore the previous search.
    if (isNewRun) {
      state.last = null;
      runPendingRequest();
    } else if (state.last) {
      renderResults(state.last);
    }
    $('#refineButton')?.addEventListener('click', () => {
      const feedback = $('#feedback')?.value.trim();
      if (!feedback || !state.last) return;
      const base = { ...(state.last.request || buildRequest(state.last.summary || '', null, state.last.intent || 'discover')), feedback };
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(base));
      window.location.href = resultsRunUrl(base.intent);
    });
    $('#feedback')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        $('#refineButton')?.click();
      }
    });
  }

  function initSaved() { renderSavedPage(); }

  function initTasteEditor() {
    state.tasteDraft = cloneProfile(state.profile);
    renderTastePage();
    $('#tasteInput')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',') {
        event.preventDefault();
        addDraftReference(event.target.value);
        event.target.value = '';
        renderTastePage();
        markTasteDraftChanged();
      }
    });
    $('#tasteInput')?.addEventListener('blur', (event) => {
      if (event.target.value.trim()) {
        addDraftReference(event.target.value);
        event.target.value = '';
        renderTastePage();
        markTasteDraftChanged();
      }
    });
    $('#avoidInput')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',') {
        event.preventDefault();
        addDraftAvoid(event.target.value);
        event.target.value = '';
        renderTastePage();
        markTasteDraftChanged();
      }
    });
    $('#avoidInput')?.addEventListener('blur', (event) => {
      if (event.target.value.trim()) {
        addDraftAvoid(event.target.value);
        event.target.value = '';
        renderTastePage();
        markTasteDraftChanged();
      }
    });
    $('#tasteNotes')?.addEventListener('input', (event) => {
      if (!state.tasteDraft) state.tasteDraft = cloneProfile(state.profile);
      state.tasteDraft.notes = event.target.value;
      markTasteDraftChanged();
    });
    $('#discoveryLevel')?.addEventListener('input', (event) => {
      if (!state.tasteDraft) state.tasteDraft = cloneProfile(state.profile);
      state.tasteDraft.discoveryLevel = Number(event.target.value);
      const label = $('#discoveryValue');
      if (label) label.textContent = `${state.tasteDraft.discoveryLevel}%`;
      markTasteDraftChanged();
    });
    $('#saveTaste')?.addEventListener('click', async () => {
      await saveTasteDraft();
      const button = $('#saveTaste');
      if (button) {
        button.textContent = 'Saved';
        window.setTimeout(() => { button.textContent = 'Save my taste'; }, 1400);
      }
    });
  }

  let pendingTasteNavigation = null;
  function isTastePanelActive() {
    const panel = $('#tastePanel');
    return Boolean(panel && !panel.hidden);
  }

  function hideTasteUnsavedModal() {
    const modal = $('#tasteUnsavedModal');
    if (modal) modal.hidden = true;
  }

  function showTasteUnsavedModal(action) {
    pendingTasteNavigation = action;
    const modal = $('#tasteUnsavedModal');
    if (!modal) return;
    modal.hidden = false;
    modal.querySelector('[role="dialog"]')?.focus();
    $('#saveTasteAndContinue')?.focus();
  }

  function requestTasteNavigation(action) {
    if (isTastePanelActive() && tasteDraftIsDirty()) {
      showTasteUnsavedModal(action);
      return;
    }
    action();
  }

  function installTasteNavigationGuard() {
    document.addEventListener('click', (event) => {
      const anchor = event.target.closest('a[href]');
      if (!anchor || anchor.target === '_blank' || !isTastePanelActive() || !tasteDraftIsDirty()) return;
      const href = anchor.getAttribute('href') || '';
      if (!href || href.startsWith('#') || href.toLowerCase().startsWith('javascript:')) return;
      event.preventDefault();
      event.stopPropagation();
      requestTasteNavigation(() => window.location.assign(anchor.href));
    }, true);
    window.addEventListener('beforeunload', (event) => {
      if (!isTastePanelActive() || !tasteDraftIsDirty()) return;
      event.preventDefault();
      event.returnValue = '';
    });
    $('#saveTasteAndContinue')?.addEventListener('click', async () => {
      const action = pendingTasteNavigation;
      await saveTasteDraft();
      pendingTasteNavigation = null;
      hideTasteUnsavedModal();
      action?.();
    });
    $('#discardTasteAndContinue')?.addEventListener('click', () => {
      const action = pendingTasteNavigation;
      pendingTasteNavigation = null;
      discardTasteDraft();
      hideTasteUnsavedModal();
      action?.();
    });
    $('#stayOnTaste')?.addEventListener('click', () => {
      pendingTasteNavigation = null;
      hideTasteUnsavedModal();
      $('#tasteTab')?.focus();
    });
  }


  function initTaste() {
    window.location.href = '/dashboard#taste';
  }


  window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    const params = new URLSearchParams(window.location.search);
    if (window.ART_CONCIERGE_PAGE !== 'results' || params.get('run') !== '1') return;
    // If navigation restored a cached Results document while a newer Concierge
    // request is waiting in session storage, rerun that request instead of
    // leaving the restored document's old result cards on screen.
    if (sessionStorage.getItem('artConciergePendingRequest')) {
      state.last = null;
      runPendingRequest();
    }
  });

  async function initializeArtCo() {
    await hydrateAppState();
    applyThemeControls();
    setupGlobalControls();
    setActiveNav();
    handleGlobalClicks();

    switch (window.ART_CONCIERGE_PAGE) {
      case 'home': initHome(); break;
      case 'dashboard': initDashboard(); break;
      case 'concierge': await initConcierge(); break;
      case 'results': initResults(); break;
      case 'saved': initSaved(); break;
      case 'taste': initTaste(); break;
      case 'checkout': initCheckoutStatus(); break;
      default: break;
    }
  }
  initializeArtCo();
})();

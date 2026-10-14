(() => {
  const STATE_VERSION = 6;

  const state = {
    profile: { loves: [], avoids: [], notes: '', discoveryLevel: 50, updatedAt: null },
    saved: [],
    recent: [],
    last: null,
    preferences: { theme: 'light', fontScale: 'md' },
    intent: new URLSearchParams(window.location.search).get('intent') || 'discover',
    inspiration: { selectedIds: [], images: [], preparing: false },
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const INSPIRATION_DB_NAME = 'artCoInspirationDB';
  const INSPIRATION_STORE = 'images';
  const INSPIRATION_SETTINGS_STORE = 'settings';
  const MAX_REQUEST_IMAGES = 5;
  const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;
  const MAX_COMPRESSED_BYTES = 360 * 1024;
  const activeInspirationUrls = new Set();
  let inspirationDbPromise = null;

  function openInspirationDb() {
    if (!('indexedDB' in window)) return Promise.reject(new Error('This browser does not support local app preferences.'));
    if (inspirationDbPromise) return inspirationDbPromise;
    inspirationDbPromise = new Promise((resolve, reject) => {
      const request = window.indexedDB.open(INSPIRATION_DB_NAME, 3);
      request.onupgradeneeded = () => {
        const db = request.result;
        // Version 3 removes the old persistent inspiration-image store. Images are
        // request-only and must not survive a search or browser navigation.
        if (db.objectStoreNames.contains(INSPIRATION_STORE)) db.deleteObjectStore(INSPIRATION_STORE);
        if (!db.objectStoreNames.contains(INSPIRATION_SETTINGS_STORE)) {
          db.createObjectStore(INSPIRATION_SETTINGS_STORE, { keyPath: 'key' });
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

  async function deleteInspirationSetting(key) {
    const db = await openInspirationDb();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(INSPIRATION_SETTINGS_STORE, 'readwrite');
      transaction.objectStore(INSPIRATION_SETTINGS_STORE).delete(key);
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(new Error('Could not clear old inspiration metadata.'));
    });
  }

  function stripInspirationPayload(request) {
    if (!request || typeof request !== 'object') return request;
    const safe = { ...request };
    delete safe.inspiration_images;
    delete safe.inspiration_image_ids;
    return safe;
  }

  function appStateSnapshot() {
    return {
      version: STATE_VERSION,
      profile: state.profile,
      saved: state.saved,
      recent: state.recent,
      last: state.last,
      preferences: state.preferences,
    };
  }

  function persistAppState() {
    putInspirationSetting('appState', appStateSnapshot()).catch((error) => {
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
        state.recent = Array.isArray(existing.recent) ? existing.recent.slice(0, 8).map((item) => ({
          ...item,
          request: stripInspirationPayload(item.request),
          data: item.data && typeof item.data === 'object' ? {
            ...item.data, request: stripInspirationPayload(item.data.request),
          } : item.data,
        })) : [];
        state.last = existing.last || null;
        if (state.last?.request) state.last.request = stripInspirationPayload(state.last.request);
        state.preferences = {
          theme: existing.preferences?.theme === 'dark' ? 'dark' : 'light',
          fontScale: ['sm', 'md', 'lg'].includes(existing.preferences?.fontScale) ? existing.preferences.fontScale : 'md',
        };
      } else {
        // IndexedDB stores profile/app state only. Inspiration images are never persisted.
        await putInspirationSetting('appState', appStateSnapshot());
      }
      state.inspiration.images = [];
      state.inspiration.selectedIds = [];
      // Delete legacy image-selection metadata and rewrite any old history without image IDs.
      await deleteInspirationSetting('selectedIds');
      await putInspirationSetting('appState', appStateSnapshot());
    } catch (error) {
      // Continue with the in-memory defaults if browser storage is unavailable.
      // No server account or cross-device sync is implied.
      console.warn('ArtCo could not load its browser state.', error);
      if (/Close another ArtCo tab/i.test(String(error?.message || ''))) {
        const status = $('#inspirationMessage');
        if (status) status.textContent = 'Close other ArtCo tabs and reload to finish removing the old saved-image library. New uploads are not saved.';
      }
    }
  }

  function saveSelectedInspiration() {
    // Selection is page memory only. It is intentionally not written to IndexedDB.
    state.inspiration.images = state.inspiration.images.slice(0, MAX_REQUEST_IMAGES);
    state.inspiration.selectedIds = state.inspiration.images.map((image) => image.id);
  }

  function clearCurrentInspirationImages() {
    clearInspirationObjectUrls();
    state.inspiration.images = [];
    state.inspiration.selectedIds = [];
    state.inspiration.preparing = false;
    const selectedWrap = $('#selectedInspirationList');
    if (selectedWrap) selectedWrap.innerHTML = '';
    const files = $('#inspirationFiles');
    if (files) files.value = '';
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

  function intentLabel(intent) {
    return {
      discover: 'Discover something new',
      find: 'Find something specific',
      taste: 'Develop my taste',
      curate: 'Curate a set of works',
      buy: 'Find art to buy',
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

  function renderInspirationUI() {
    const selectedWrap = $('#selectedInspirationList');
    if (!selectedWrap) return;
    clearInspirationObjectUrls();
    saveSelectedInspiration();
    selectedWrap.innerHTML = state.inspiration.images.map((record) => {
      const url = imageObjectUrl(record);
      return `<article class="selected-inspiration-item">
        <img src="${escapeAttribute(url)}" alt="${escapeAttribute(record.name)}" loading="lazy" decoding="async">
        <div class="selected-inspiration-copy"><span>${escapeHtml(record.name)}</span><button class="text-button" type="button" data-remove-selected-image="${escapeAttribute(record.id)}">Remove</button></div>
      </article>`;
    }).join('');
    const status = $('#inspirationMessage');
    if (status && state.inspiration.images.length && !status.textContent) {
      status.textContent = `${state.inspiration.images.length} image${state.inspiration.images.length === 1 ? '' : 's'} attached to this search only. ArtCo discards them when the search finishes.`;
    }
  }

  async function handleInspirationUpload(fileList) {
    const files = [...(fileList || [])];
    if (!files.length) return;
    const status = $('#inspirationMessage');
    const errors = [];
    let added = 0;
    for (const file of files) {
      if (state.inspiration.images.length >= MAX_REQUEST_IMAGES) {
        errors.push(`A request can use at most ${MAX_REQUEST_IMAGES} images. Remove an image before adding another.`);
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
        added += 1;
      } catch (error) {
        errors.push(error.message || `${safeImageName(file.name)} could not be prepared.`);
      }
    }
    saveSelectedInspiration();
    renderInspirationUI();
    if (status) {
      const successText = added
        ? `${added} image${added === 1 ? '' : 's'} attached to this search. Images are not saved to your profile and will be discarded when the search finishes.`
        : '';
      status.textContent = [successText, ...errors].filter(Boolean).join(' ');
    }
  }

  function renderDashboard() {
    const recent = $('#dashboardRecent');
    const saved = $('#dashboardSaved');
    const savedEmpty = $('#dashboardSavedEmpty');
    if (!recent && !saved) return;

    if (recent) {
      recent.innerHTML = state.recent.length
        ? state.recent.slice(0, 5).map(recentMarkup).join('')
        : '<p class="empty-state">Your recent conversations will appear here after your first search.</p>';
    }

    if (saved) {
      const works = state.saved.slice(0, 3);
      saved.innerHTML = works.map((work) => {
        const id = escapeAttribute(work.id);
        const savedImageUrl = work.display_image_url || work.image_url || artworkPreviewUrl(work.detail_url);
        const image = savedImageUrl
          ? `<img data-artwork-image data-detail-url="${escapeAttribute(work.detail_url || '')}" ${work.detail_url && savedImageUrl === artworkPreviewUrl(work.detail_url) ? 'data-tried-detail-preview="true"' : ''} src="${escapeAttribute(savedImageUrl)}" alt="${escapeAttribute(work.title || 'Artwork')} by ${escapeAttribute(work.artist || 'Unknown artist')}" loading="eager" decoding="async" fetchpriority="high" referrerpolicy="strict-origin-when-cross-origin">`
          : '<div class="saved-thumb no-image">Preview unavailable</div>';
        return `<button class="saved-thumb" type="button" data-saved-id="${id}" aria-label="Open ${escapeAttribute(work.title || 'saved artwork')}">${image}</button>`;
      }).join('');
      bindArtworkImageFallbacks(saved);
      if (savedEmpty) savedEmpty.hidden = works.length > 0;
    }

    renderTastePage();

    const overviewTab = $('#overviewTab');
    const tasteTab = $('#tasteTab');
    const overviewPanel = $('#overviewPanel');
    const tastePanel = $('#tastePanel');
    const activateTab = (tabName, moveFocus = false) => {
      const isTaste = tabName === 'taste';
      overviewTab?.classList.toggle('is-active', !isTaste);
      tasteTab?.classList.toggle('is-active', isTaste);
      overviewTab?.setAttribute('aria-selected', String(!isTaste));
      tasteTab?.setAttribute('aria-selected', String(isTaste));
      overviewTab?.setAttribute('tabindex', isTaste ? '-1' : '0');
      tasteTab?.setAttribute('tabindex', isTaste ? '0' : '-1');
      if (overviewPanel) { overviewPanel.hidden = isTaste; overviewPanel.classList.toggle('is-active', !isTaste); }
      if (tastePanel) { tastePanel.hidden = !isTaste; tastePanel.classList.toggle('is-active', isTaste); }
      if (moveFocus) (isTaste ? tasteTab : overviewTab)?.focus();
    };
    overviewTab?.addEventListener('click', () => activateTab('overview'));
    tasteTab?.addEventListener('click', () => activateTab('taste'));
    [overviewTab, tasteTab].forEach((tab) => tab?.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') { event.preventDefault(); activateTab(tab === overviewTab ? 'taste' : 'overview', true); }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') { event.preventDefault(); activateTab(tab === overviewTab ? 'taste' : 'overview', true); }
      if (event.key === 'Home') { event.preventDefault(); activateTab('overview', true); }
      if (event.key === 'End') { event.preventDefault(); activateTab('taste', true); }
    }));
    const hash = window.location.hash.toLowerCase();
    activateTab(hash === '#taste' ? 'taste' : 'overview');
  }

  function renderHome() {
    // The landing page is intentionally focused on the product proposition and primary CTA.
  }

  function intentCopy(intent) {
    return {
      discover: ['What are you looking for?', 'Start with what you love, then let ArtCo take the search somewhere new.', 'I love punk music and bold patterns. Find art with a similar energy, but take me somewhere unexpected.'],
      find: ['What are you looking for?', 'Describe the artist, artwork, style, or visual detail you want to find.', 'Find an artist or artwork with this style, colour palette, or subject.'],
      taste: ['What are you curious about?', 'Explore what draws you to certain art, and discover new directions from there.', 'Help me understand why I am drawn to bold patterns, expressive colour, and unusual textures.'],
      curate: ['What should work together?', 'Describe the space, mood, or idea you want the works to share.', 'Find three works for a small living room, with warm colours and an experimental feel.'],
      buy: ['What would you like to buy?', 'Share your budget, location, or the kind of work you hope to acquire.', 'Find original art under $500 that I can buy in Canada, with bold colour and geometric forms.'],
      learn: ['What would you like to understand?', 'Ask about an artist, artwork, movement, or idea.', 'Explain abstract expressionism in plain language and show me works that help illustrate it.'],
    }[intent] || ['What are you looking for?', 'Start with what you love, then let ArtCo take the search somewhere new.', 'Tell me what you are looking for.'];
  }

  function setIntent(intent) {
    state.intent = intent;
    const [, , placeholder] = intentCopy(intent);
    const titleNode = $('#conciergeTitle');
    const ledeNode = $('#conciergeLede');
    const input = $('#requestInput');
    const hint = $('#requestHint');
    if (titleNode) titleNode.textContent = 'Your art concierge';
    if (ledeNode) ledeNode.textContent = 'Select a service, then describe what you need.';
    if (input) input.placeholder = placeholder;
    if (hint) hint.textContent = 'Add as much or as little detail as you like.';
  }

  function addProfileReference(value) {
    const clean = value.trim().replace(/,$/, '');
    if (!clean || state.profile.loves.includes(clean) || state.profile.loves.length >= 12) return;
    state.profile.loves.push(clean);
    saveProfile();
  }

  function addProfileAvoid(value) {
    const clean = String(value || '').trim().replace(/,$/, '');
    if (!clean || /^(unknown|anonymous|various) artist$/i.test(clean)) return;
    if (state.profile.avoids.some((item) => item.toLowerCase() === clean.toLowerCase())) return;
    if (state.profile.avoids.length >= 12) state.profile.avoids.shift();
    state.profile.avoids.push(clean);
    state.profile.loves = state.profile.loves.filter((item) => item.toLowerCase() !== clean.toLowerCase());
    saveProfile();
  }

  function learnFromFeedback(feedback, artwork) {
    if (!artwork) return;
    const artist = String(artwork.artist || '').trim();
    const knownArtist = artist && !/^(unknown|anonymous|various) artist$/i.test(artist);
    if (/^More like/i.test(feedback || '') && knownArtist) {
      if (!state.profile.loves.some((item) => item.toLowerCase() === artist.toLowerCase())) {
        state.profile.loves.push(artist);
      }
      state.profile.avoids = state.profile.avoids.filter((item) => item.toLowerCase() !== artist.toLowerCase());
      state.profile.loves = state.profile.loves.slice(-12);
      saveProfile();
    } else if (/^Not for me/i.test(feedback || '') && knownArtist) {
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
    if (/learn.*taste|develop.*taste|understand.*like/.test(lower)) return 'taste';
    if (/specific|looking for|need a|find me/.test(lower)) return 'find';
    if (/artist|artwork|movement|history|about|learn|explain|who is|what is/.test(lower)) return 'learn';
    return 'discover';
  }

  function buildRequest(prompt, feedback = null, explicitIntent = null) {
    const inferred = explicitIntent || inferIntent(prompt);
    const budget = parseBudget(prompt);
    return {
      intent: inferred,
      loves: [...state.profile.loves],
      avoid_references: [...state.profile.avoids],
      additional_interests: [],
      art_interests: [],
      mediums: [],
      budget_min: null,
      budget_max: budget,
      room: null,
      size_preference: null,
      preferred_market: null,
      discovery_level: state.profile.discoveryLevel,
      purchase_required: inferred === 'buy',
      goal: prompt.trim() || null,
      number_of_works: inferred === 'curate' ? 3 : 1,
      feedback,
      exclude_artwork_ids: [],
      inspiration_image_ids: state.inspiration.images.map((image) => image.id).slice(0, MAX_REQUEST_IMAGES),
      inspiration_images: [],
    };
  }

  async function beginRequest(prompt) {
    const clean = String(prompt || '').trim();
    if (state.inspiration.preparing) return;
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
    const inferred = params.has('intent') ? state.intent : inferIntent(clean);
    const request = buildRequest(clean, null, inferred);
    const submit = $('#requestForm button[type="submit"]');
    state.inspiration.preparing = true;
    if (submit) { submit.disabled = true; submit.textContent = 'Preparing search…'; }
    const status = $('#inspirationMessage');
    try {
      const selectedImages = state.inspiration.images.slice(0, MAX_REQUEST_IMAGES);
      request.inspiration_images = await Promise.all(selectedImages.map(async (record) => ({
        image_id: record.id,
        name: record.name,
        notes: record.notes || '',
        mime_type: record.mimeType || record.blob.type || 'image/jpeg',
        data: await blobToBase64(record.blob),
      })));
      request.inspiration_image_ids = request.inspiration_images.map((image) => image.image_id);
      // The payload exists only to cross this single navigation. The results page
      // consumes and removes it before sending the one search request.
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(request));
      clearCurrentInspirationImages();
      window.location.assign(`/results?from=concierge&intent=${encodeURIComponent(inferred)}&run=1`);
    } catch (error) {
      state.inspiration.preparing = false;
      if (submit) { submit.disabled = false; submit.textContent = 'Ask my concierge →'; }
      if (status) status.textContent = error?.name === 'QuotaExceededError'
        ? 'These images could not be prepared for this search. Try fewer or smaller images.'
        : (error.message || 'ArtCo could not prepare this search. Please try again.');
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
    try {
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(request));
    } catch (error) {
      const count = $('#savedCount');
      if (count) count.textContent = 'Could not start the search in this browser tab. Please allow session storage and try again.';
      return;
    }
    window.location.assign('/results?intent=discover&run=1');
  }

  function beginNotForMeSearch(artwork, button = null) {
    if (!artwork) return;
    const title = String(artwork.title || 'this artwork').trim();
    const artist = String(artwork.artist || '').trim();
    const knownArtist = artist && !/^(unknown|anonymous|various) artist$/i.test(artist);
    const prompt = knownArtist
      ? `Show me different art from ${artist}. I did not like “${title}” by ${artist}; exclude this work and avoid this artist. Recommend different artists and a different direction.`
      : `Show me different art from “${title}”. I did not like this work; exclude it and recommend alternatives with a different visual direction.`;
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
    if (button) { button.disabled = true; button.textContent = 'Updating…'; }
    try {
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(request));
      window.location.assign('/results?intent=discover&run=1');
    } catch (error) {
      if (button) { button.disabled = false; button.textContent = 'Not for me'; }
      const message = $('#resultsSummary') || $('#savedCount');
      if (message) message.textContent = 'Could not apply that feedback. Please try again.';
    }
  }

  async function runPendingRequest() {
    const resultsPage = $('#resultsPage');
    if (!resultsPage) return false;
    let raw = sessionStorage.getItem('artConciergePendingRequest');
    if (!raw) return false;
    let payload;
    try { payload = JSON.parse(raw); } catch { sessionStorage.removeItem('artConciergePendingRequest'); return false; }
    // Consume the one-time handoff immediately; image bytes are no longer in browser storage.
    sessionStorage.removeItem('artConciergePendingRequest');
    raw = null;

    const resultsHeading = $('#resultsHeading');
    const resultsSummary = $('#resultsSummary');
    const resultsMeta = $('#resultsMeta');
    const grid = $('#artGrid');
    const notForSaleSection = $('#notForSaleSection');

    resultsHeading.textContent = 'I’m researching that now.';
    resultsSummary.textContent = 'Your concierge is connecting your request with your existing taste and looking across the available art sources.';
    resultsMeta.textContent = '';
    grid.innerHTML = '<p class="empty-state">Searching and narrowing the field…</p>';
    notForSaleSection.hidden = true;

    const requestNotices = [];
    let apiPayload = null;
    try {
      apiPayload = {
        ...payload,
        inspiration_images: Array.isArray(payload.inspiration_images) ? payload.inspiration_images.slice(0, MAX_REQUEST_IMAGES) : [],
        inspiration_image_ids: [],
      };
      apiPayload.inspiration_image_ids = apiPayload.inspiration_images.map((image) => image.image_id);
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
      const historyRequest = stripInspirationPayload(payload);
      data.request = historyRequest;
      saveLastResult(data);
      saveRecent(historyRequest, data);
      renderResults(data);
      if (requestNotices.length) {
        const summary = $('#resultsSummary');
        if (summary) summary.textContent = `${summary.textContent} ${requestNotices.join(' ')}`;
      }
      return true;
    } catch (error) {
      resultsHeading.textContent = 'I couldn’t complete that search.';
      resultsSummary.textContent = error.message || 'Please try again.';
      resultsMeta.textContent = '';
      grid.innerHTML = '<p class="empty-state">Check your connected services, then try the request again.</p>';
      return false;
    } finally {
      // Release transient image bytes after this one request, on success or failure.
      if (apiPayload) apiPayload.inspiration_images = [];
      if (payload && typeof payload === 'object') {
        payload.inspiration_images = [];
        payload.inspiration_image_ids = [];
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

  function bindArtworkImageFallbacks(root) {
    if (!root) return;
    root.querySelectorAll('img[data-artwork-image]').forEach((image) => {
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
        const placeholder = document.createElement('div');
        placeholder.className = 'art-placeholder';
        placeholder.textContent = 'Preview unavailable';
        placeholder.setAttribute('role', 'img');
        placeholder.setAttribute('aria-label', 'Artwork preview unavailable; open the source page to view the work');
        image.replaceWith(placeholder);
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
    const imageDisplayUrl = work.display_image_url || directImageUrl || pagePreviewUrl;
    const previewIsInitial = Boolean(pagePreviewUrl && imageDisplayUrl === pagePreviewUrl);
    const image = imageDisplayUrl
      ? `<img data-artwork-image data-detail-url="${escapeAttribute(work.detail_url || '')}" ${work.display_image_url && work.display_image_url !== directImageUrl && directImageUrl ? `data-original-src="${escapeAttribute(directImageUrl)}"` : ''} ${previewIsInitial ? 'data-tried-detail-preview="true"' : ''} src="${escapeAttribute(imageDisplayUrl)}" alt="${escapeAttribute(work.title || 'Artwork')} by ${escapeAttribute(work.artist || 'Unknown artist')}" loading="eager" decoding="async" fetchpriority="high" referrerpolicy="strict-origin-when-cross-origin">`
      : '<div class="art-placeholder">Preview unavailable</div>';
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
          <div class="art-meta">${escapeHtml(meta || work.source || '')}</div>
          <div class="art-reason">${escapeHtml(reasons)}</div>
          <div class="art-footer"><span class="availability ${availability}">${available ? 'AVAILABLE TO ACQUIRE' : 'NOT FOR SALE'}</span><span class="art-source">${escapeHtml(work.source || '')}</span></div>
          <div class="art-actions">
            ${link}
            <button class="card-action save-action" type="button" data-id="${id}" aria-pressed="${saved}">${saved ? 'Saved' : 'Save'}</button>
            <button class="card-action feedback-action" type="button" data-id="${id}" data-feedback="More like ${escapeAttribute(work.artist || work.title)}">More like this</button>
            <button class="card-action feedback-action" type="button" data-id="${id}" data-feedback="Not for me: ${escapeAttribute(work.artist || work.title)}">Not for me</button>
          </div>
        </div>
      </article>`;
  }

  function renderResults(data) {
    const intent = data.intent || state.intent;
    const labels = {
      discover: 'Works worth a closer look.',
      find: 'The closest fits I found.',
      taste: 'Artists and works to explore.',
      curate: 'Works that make sense together.',
      buy: 'Art you can act on now.',
      learn: 'Art worth understanding.',
    };
    $('#resultsHeading').textContent = labels[intent] || labels.discover;
    $('#resultsSummary').textContent = data.summary || '';
    const available = data.purchase_available || 0;
    const results = (data.results || []).slice(0, 3);
    const notForSale = intent === 'buy'
      ? (data.not_for_sale || []).slice(0, Math.max(0, 3 - results.length))
      : [];
    const visibleCount = results.length + notForSale.length;
    const visiblePurchasable = results.filter((work) => work.availability === 'available' && work.source_kind === 'commercial').length;
    $('#resultsMeta').textContent = intent === 'buy'
      ? `${visibleCount} work${visibleCount === 1 ? '' : 's'} shown · ${visiblePurchasable} available to acquire`
      : `${visibleCount} recommendation${visibleCount === 1 ? '' : 's'} shown`;
    if (results.length) {
      $('#artGrid').innerHTML = results.map(renderCard).join('');
      bindArtworkImageFallbacks($('#artGrid'));
    } else {
      const diagnostic = data.diagnostics || {};
      const commercialConfigured = (diagnostic.commercial_sources_configured || []).length > 0;
      let emptyMessage = intent === 'buy'
        ? (commercialConfigured
          ? 'I could not find purchase inventory in the connected commercial sources.'
          : 'No commercial art source is connected yet. Connect an acquisition source to search art available to buy.')
        : 'The connected art collections did not return a match for that wording. Try a broader art style, artist, subject, or medium.';
      const failedSources = (diagnostic.source_diagnostics || [])
        .filter((source) => source.configured && (source.errors || []).length)
        .map((source) => `${source.provider} (${source.errors[0]})`);
      if (failedSources.length) {
        emptyMessage += ` Sources with connection issues: ${failedSources.join(', ')}.`;
      }
      $('#artGrid').innerHTML = `<p class="empty-state">${escapeHtml(emptyMessage)}</p>`;
    }

    $('#notForSaleSection').hidden = !notForSale.length;
    if (notForSale.length) {
      $('#notForSaleGrid').innerHTML = notForSale.map(renderCard).join('');
      bindArtworkImageFallbacks($('#notForSaleGrid'));
      $('#notForSaleHint').textContent = intent === 'buy'
        ? 'Additional works related to your brief, but not currently available to acquire.'
        : 'Additional works from museum and institutional collections. These are not available for purchase.';
    }
    renderVisualInspirationSummary(data.visual_inspiration || {});
    renderTasteSummary(data);
  }

  function renderVisualInspirationSummary(inspiration) {
    const panel = $('#visualInspirationSummary');
    if (!panel) return;
    const count = Number(inspiration.image_count || 0);
    if (!count) { panel.hidden = true; panel.innerHTML = ''; return; }
    panel.hidden = false;
    const heading = inspiration.analyzed ? 'How your images guided the search' : 'Your images were received';
    const detail = inspiration.analyzed
      ? (inspiration.summary || 'ArtCo used the visual characteristics in your images to guide the artwork search.')
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
    const chips = $('#tasteChips');
    const avoids = $('#avoidChips');
    const notes = $('#tasteNotes');
    const slider = $('#discoveryLevel');
    const updated = $('#tasteUpdated');
    if (!chips || !notes) return;
    chips.innerHTML = state.profile.loves.map((value, index) => `
      <span class="chip">${escapeHtml(value)} <button type="button" data-remove-index="${index}" aria-label="Remove ${escapeAttribute(value)} from things you love">×</button></span>
    `).join('');
    if (avoids) avoids.innerHTML = state.profile.avoids.map((value, index) => `
      <span class="chip">${escapeHtml(value)} <button type="button" data-remove-list="avoids" data-remove-index="${index}" aria-label="Remove ${escapeAttribute(value)} from things you avoid">×</button></span>
    `).join('');
    notes.value = state.profile.notes;
    if (slider) slider.value = String(state.profile.discoveryLevel);
    if (updated) updated.textContent = state.profile.updatedAt ? `Updated ${new Date(state.profile.updatedAt).toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' })}` : 'Not yet saved';
  }

  async function startCheckout(id, button) {
    const message = $('#purchaseMessage');
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
      if (!response.ok || !data.success || !data.checkout_url) {
        throw new Error(data.detail || 'Checkout is not available for this work.');
      }
      window.location.assign(data.checkout_url);
    } catch (error) {
      if (message) message.textContent = error.message || 'Checkout could not be started.';
      if (button) {
        button.disabled = false;
        button.textContent = 'Buy securely';
      }
    }
  }

  function handleGlobalClicks() {
    document.addEventListener('click', (event) => {
      const removeSelectedImage = event.target.closest('[data-remove-selected-image]');
      if (removeSelectedImage) {
        state.inspiration.images = state.inspiration.images.filter((image) => image.id !== removeSelectedImage.dataset.removeSelectedImage);
        saveSelectedInspiration();
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
      if (buy) { startCheckout(buy.dataset.id, buy); return; }

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
          const request = { ...item.request };
          delete request.inspiration_images;
          delete request.inspiration_image_ids;
          sessionStorage.setItem('artCoResumeRequest', JSON.stringify(request));
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
        const list = remove.dataset.removeList === 'avoids' ? state.profile.avoids : state.profile.loves;
        list.splice(Number(remove.dataset.removeIndex), 1);
        saveProfile();
        renderTastePage();
      }
    });
  }

  function initHome() { renderHome(); }
  function initDashboard() {
    renderDashboard();
    initTasteEditor();
  }

  async function initConcierge() {
    const params = new URLSearchParams(window.location.search);
    state.inspiration.images = [];
    state.inspiration.selectedIds = [];
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
    if (state.last) renderResults(state.last);
    const params = new URLSearchParams(window.location.search);
    if (params.get('run') === '1') runPendingRequest();
    $('#refineButton')?.addEventListener('click', () => {
      const feedback = $('#feedback')?.value.trim();
      if (!feedback || !state.last) return;
      const base = { ...(state.last.request || buildRequest(state.last.summary || '', null, state.last.intent || 'discover')), feedback };
      sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(base));
      window.location.href = `/results?intent=${encodeURIComponent(base.intent)}&run=1`;
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
    renderTastePage();
    $('#tasteInput')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',') {
        event.preventDefault();
        addProfileReference(event.target.value);
        event.target.value = '';
        renderTastePage();
      }
    });
    $('#tasteInput')?.addEventListener('blur', (event) => {
      if (event.target.value.trim()) {
        addProfileReference(event.target.value);
        event.target.value = '';
        renderTastePage();
      }
    });
    $('#avoidInput')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',') {
        event.preventDefault();
        addProfileAvoid(event.target.value);
        event.target.value = '';
        renderTastePage();
      }
    });
    $('#avoidInput')?.addEventListener('blur', (event) => {
      if (event.target.value.trim()) {
        addProfileAvoid(event.target.value);
        event.target.value = '';
        renderTastePage();
      }
    });
    $('#tasteNotes')?.addEventListener('input', (event) => {
      state.profile.notes = event.target.value;
      saveProfile();
    });
    $('#discoveryLevel')?.addEventListener('input', (event) => {
      state.profile.discoveryLevel = Number(event.target.value);
      saveProfile();
      const label = $('#discoveryValue');
      if (label) label.textContent = `${state.profile.discoveryLevel}%`;
    });
    $('#saveTaste')?.addEventListener('click', () => {
      saveProfile();
      renderTastePage();
      const button = $('#saveTaste');
      if (button) {
        button.textContent = 'Saved';
        window.setTimeout(() => { button.textContent = 'Save my taste'; }, 1400);
      }
    });
  }

  function initTaste() {
    window.location.href = '/dashboard#taste';
  }


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
      default: break;
    }
  }
  initializeArtCo();
})();

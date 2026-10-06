(() => {
  const storage = {
    profile: 'artConciergeProfile',
    saved: 'artConciergeSaved',
    recent: 'artConciergeRecent',
    last: 'artConciergeLastResult',
  };

  const readJson = (key, fallback) => {
    try {
      const value = JSON.parse(localStorage.getItem(key) || 'null');
      return value ?? fallback;
    } catch {
      return fallback;
    }
  };

  const profile = readJson(storage.profile, {});
  const state = {
    profile: {
      loves: Array.isArray(profile.loves) ? profile.loves.slice(0, 12) : [],
      notes: String(profile.notes || ''),
      discoveryLevel: Number(profile.discoveryLevel ?? 50),
    },
    saved: readJson(storage.saved, []).slice(0, 40),
    recent: readJson(storage.recent, []).slice(0, 8),
    last: readJson(storage.last, null),
    intent: new URLSearchParams(window.location.search).get('intent') || 'discover',
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function saveProfile() {
    localStorage.setItem(storage.profile, JSON.stringify(state.profile));
  }

  function saveLastResult(data) {
    state.last = data;
    localStorage.setItem(storage.last, JSON.stringify(data));
  }

  function saveRecent(request, result) {
    const item = {
      id: `${Date.now()}`,
      intent: request.intent,
      prompt: request.goal || intentLabel(request.intent),
      savedAt: new Date().toISOString(),
      data: result,
    };
    state.recent = [item, ...state.recent.filter((entry) => entry.prompt !== item.prompt)].slice(0, 8);
    localStorage.setItem(storage.recent, JSON.stringify(state.recent));
  }

  function intentLabel(intent) {
    return {
      discover: 'Discover something new',
      find: 'Find something specific',
      taste: 'Develop my taste',
      curate: 'Curate a set of works',
      buy: 'Find art to buy',
      keep_discovering: 'Keep discovering',
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
    const theme = localStorage.getItem('artConciergeTheme') || 'light';
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    const font = localStorage.getItem('artConciergeFontScale') || 'md';
    document.documentElement.style.setProperty('--scale', ({ sm: .92, md: 1, lg: 1.12 }[font] || 1));
    const select = $('#fontScale');
    if (select) select.value = font;
    const button = $('#themeToggle');
    if (button) button.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  }

  function setupGlobalControls() {
    $('#themeToggle')?.addEventListener('click', () => {
      const next = (document.documentElement.dataset.theme || 'light') === 'dark' ? 'light' : 'dark';
      localStorage.setItem('artConciergeTheme', next);
      applyThemeControls();
    });
    $('#fontScale')?.addEventListener('change', (event) => {
      const value = event.target.value;
      localStorage.setItem('artConciergeFontScale', value);
      document.documentElement.style.setProperty('--scale', ({ sm: .92, md: 1, lg: 1.12 }[value] || 1));
    });
  }

  function recentMarkup(item) {
    return `
      <article class="recent-item">
        <div class="recent-copy">
          <div class="recent-title">${escapeHtml(item.prompt)}</div>
          <div class="recent-meta">${escapeHtml(intentLabel(item.intent))} · ${escapeHtml(new Date(item.savedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))}</div>
        </div>
        <button class="secondary-button recent-open" type="button" data-recent-id="${escapeAttribute(item.id)}">Continue</button>
      </article>`;
  }

  function renderDashboard() {
    const recent = $('#dashboardRecent');
    const saved = $('#dashboardSaved');
    const savedEmpty = $('#dashboardSavedEmpty');
    const tasteChips = $('#dashboardTasteChips');
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
        const image = work.image_url
          ? `<img src="${escapeAttribute(work.image_url)}" alt="${escapeAttribute(work.title || 'Artwork')} by ${escapeAttribute(work.artist || 'Unknown artist')}" loading="lazy">`
          : '<div class="saved-thumb no-image">Image unavailable</div>';
        return `<button class="saved-thumb" type="button" data-saved-id="${id}" aria-label="Open ${escapeAttribute(work.title || 'saved artwork')}">${image}</button>`;
      }).join('');
      if (savedEmpty) savedEmpty.hidden = works.length > 0;
    }

    if (tasteChips) {
      tasteChips.innerHTML = state.profile.loves.slice(0, 8).map((value) => `<span class="chip">${escapeHtml(value)}</span>`).join('');
    }
    const tasteCopy = $('#dashboardTasteCopy');
    if (tasteCopy && state.profile.loves.length) {
      tasteCopy.textContent = `${state.profile.loves.length} reference${state.profile.loves.length === 1 ? '' : 's'} saved. Your concierge can use them in future searches.`;
    }
  }

  function renderHome() {
    // The landing page is intentionally focused on the product proposition and primary CTA.
  }

  function renderTasteInConcierge() {
    const wrap = $('#savedTaste');
    const empty = $('#savedTasteEmpty');
    if (!wrap || !empty) return;
    wrap.innerHTML = state.profile.loves.map((value) => `<span class="chip">${escapeHtml(value)}</span>`).join('');
    empty.hidden = state.profile.loves.length > 0;
  }

  function intentCopy(intent) {
    return {
      discover: ['What are you looking for?', 'Start with what you love, then let me take the search somewhere new.'],
      find: ['What are you looking for?', 'Tell me what you have in mind. I’ll narrow the search around it.'],
      taste: ['What are you curious about?', 'Give me a few cultural references and I’ll use them to open up new directions.'],
      curate: ['What should work together?', 'Tell me about the space, mood, or idea you want to bring together.'],
      buy: ['What would you like to buy?', 'Tell me what you can spend, where you are, and how far you want me to explore.'],
      keep_discovering: ['What should I explore next?', 'I’ll build from the things you’ve already told me you like.'],
    }[intent] || ['What are you looking for?', 'Start naturally and I’ll take it from there.'];
  }

  function setIntent(intent) {
    state.intent = intent;
    const [title, lede] = intentCopy(intent);
    $('#conciergeTitle').textContent = title;
    $('#conciergeLede').textContent = lede;
    $$('#intentList button').forEach((button) => {
      if (button.dataset.intent === intent) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    });
  }

  function addProfileReference(value) {
    const clean = value.trim().replace(/,$/, '');
    if (!clean || state.profile.loves.includes(clean) || state.profile.loves.length >= 12) return;
    state.profile.loves.push(clean);
    saveProfile();
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
    if (/again|continue|keep/.test(lower)) return 'keep_discovering';
    return 'discover';
  }

  function buildRequest(prompt, feedback = null, explicitIntent = null) {
    const inferred = explicitIntent || inferIntent(prompt);
    const budget = parseBudget(prompt);
    return {
      intent: inferred,
      loves: [...state.profile.loves],
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
    };
  }

  function beginRequest(prompt) {
    const clean = String(prompt || '').trim();
    if (!clean) return;
    const inferred = state.intent && new URLSearchParams(window.location.search).has('intent') ? state.intent : inferIntent(clean);
    window.location.href = `/results?from=concierge&intent=${encodeURIComponent(inferred)}&run=1`;
    sessionStorage.setItem('artConciergePendingRequest', JSON.stringify(buildRequest(clean, null, inferred)));
  }

  async function runPendingRequest() {
    const raw = sessionStorage.getItem('artConciergePendingRequest');
    if (!raw) return false;
    sessionStorage.removeItem('artConciergePendingRequest');
    let payload;
    try { payload = JSON.parse(raw); } catch { return false; }
    const resultsPage = $('#resultsPage');
    if (!resultsPage) return false;

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

    try {
      const response = await fetch('/api/concierge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail?.[0]?.msg || detail?.detail || `Request failed (${response.status})`);
      }
      const data = await response.json();
      data.request = payload;
      saveLastResult(data);
      saveRecent(payload, data);
      renderResults(data);
      return true;
    } catch (error) {
      resultsHeading.textContent = 'I couldn’t complete that search.';
      resultsSummary.textContent = error.message || 'Please try again.';
      resultsMeta.textContent = '';
      grid.innerHTML = '<p class="empty-state">Check your connected services, then try the request again.</p>';
      return false;
    }
  }

  function renderTasteSummary(data) {
    const box = $('#tasteSummary');
    if (!box) return;
    const inputs = data.taste?.inputs || [];
    const artists = data.taste?.related_artists || [];
    const tags = [...new Set([...inputs, ...artists.slice(0, 4).map((artist) => artist.name)].filter(Boolean))].slice(0, 8);
    if (!tags.length) { box.hidden = true; return; }
    box.hidden = false;
    $('#tasteSummaryCopy').textContent = 'The search was shaped by the cultural references you have shared with your concierge.';
    $('#tasteTags').innerHTML = tags.map((tag) => `<span class="taste-tag">${escapeHtml(tag)}</span>`).join('');
  }

  function renderCard(work) {
    const available = work.availability === 'available' && work.source_kind === 'commercial';
    const availability = available ? 'available' : 'not_for_sale';
    const meta = [work.medium, work.dimensions, work.year, work.price_label].filter(Boolean).join(' · ');
    const image = work.image_url
      ? `<img src="${escapeAttribute(work.image_url)}" alt="${escapeAttribute(work.title || 'Artwork')} by ${escapeAttribute(work.artist || 'Unknown artist')}" loading="lazy">`
      : '<div class="art-placeholder">Image unavailable</div>';
    const linkLabel = available ? 'View artwork' : 'See at source';
    const link = work.detail_url
      ? `<a class="card-action" href="${escapeAttribute(work.detail_url)}" target="_blank" rel="noopener noreferrer">${linkLabel} →</a>`
      : '';
    const saved = state.saved.some((item) => item.id === work.id);
    const reasons = (work.why || []).map((item) => String(item)).join(' · ');
    const id = escapeAttribute(work.id);
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
            <button class="card-action feedback-action" type="button" data-feedback="More like ${escapeAttribute(work.artist || work.title)}">More like this</button>
            <button class="card-action feedback-action" type="button" data-feedback="Not for me: ${escapeAttribute(work.artist || work.title)}">Not for me</button>
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
      keep_discovering: 'Your next discoveries.',
    };
    $('#resultsHeading').textContent = labels[intent] || labels.discover;
    $('#resultsSummary').textContent = data.summary || '';
    const available = data.purchase_available || 0;
    $('#resultsMeta').textContent = intent === 'buy'
      ? (available ? `${available} purchase option${available === 1 ? '' : 's'} found` : 'No purchase inventory found')
      : `${data.total_found || 0} works researched`;
    const results = data.results || [];
    $('#artGrid').innerHTML = results.length
      ? results.map(renderCard).join('')
      : `<p class="empty-state">${escapeHtml(intent === 'buy' ? 'I could not find purchase inventory in the connected commercial sources.' : 'I could not find strong matches. Refine the request and I’ll search again.')}</p>`;

    const notForSale = data.not_for_sale || [];
    $('#notForSaleSection').hidden = !notForSale.length;
    if (notForSale.length) {
      $('#notForSaleGrid').innerHTML = notForSale.map(renderCard).join('');
      $('#notForSaleHint').textContent = intent === 'buy'
        ? 'These works are not currently available to acquire, but they are relevant to what you asked me to find.'
        : 'These works are useful references from institutional collections.';
    }
    renderTasteSummary(data);
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
    localStorage.setItem(storage.saved, JSON.stringify(state.saved));
    if (window.ART_CONCIERGE_PAGE === 'results' && state.last) renderResults(state.last);
    if (window.ART_CONCIERGE_PAGE === 'saved') renderSavedPage();
  }

  function renderSavedPage() {
    const grid = $('#savedGrid');
    if (!grid) return;
    if (!state.saved.length) {
      grid.innerHTML = '<p class="empty-state">You have not saved any works yet. Start a conversation with your concierge to find something worth keeping.</p>';
      return;
    }
    grid.innerHTML = state.saved.map(renderCard).join('');
  }

  function renderTastePage() {
    const chips = $('#tasteChips');
    const notes = $('#tasteNotes');
    if (!chips || !notes) return;
    chips.innerHTML = state.profile.loves.map((value, index) => `
      <span class="chip">${escapeHtml(value)} <button type="button" data-remove-index="${index}" aria-label="Remove ${escapeAttribute(value)}">×</button></span>
    `).join('');
    notes.value = state.profile.notes;
  }

  function handleGlobalClicks() {
    document.addEventListener('click', (event) => {
      const save = event.target.closest('.save-action');
      if (save) { toggleSave(save.dataset.id); return; }

      const feedback = event.target.closest('.feedback-action');
      if (feedback) {
        const input = $('#feedback');
        if (input) { input.value = feedback.dataset.feedback || ''; input.focus(); }
        return;
      }

      const recent = event.target.closest('.recent-open');
      if (recent) {
        const item = state.recent.find((entry) => entry.id === recent.dataset.recentId);
        if (item?.data) {
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
        state.profile.loves.splice(Number(remove.dataset.removeIndex), 1);
        saveProfile();
        renderTastePage();
      }
    });
  }

  function initHome() { renderHome(); }
  function initDashboard() { renderDashboard(); }

  function initConcierge() {
    setIntent(state.intent);
    renderTasteInConcierge();
    const form = $('#requestForm');
    const input = $('#requestInput');
    form?.addEventListener('submit', (event) => {
      event.preventDefault();
      beginRequest(input.value);
    });
    input?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        beginRequest(input.value);
      }
    });
    $$('#intentList button').forEach((button) => button.addEventListener('click', () => {
      const url = new URL('/concierge', window.location.origin);
      url.searchParams.set('intent', button.dataset.intent);
      window.location.href = url.toString();
    }));
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

  function initTaste() {
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
    $('#tasteNotes')?.addEventListener('input', (event) => {
      state.profile.notes = event.target.value;
      saveProfile();
    });
    $('#saveTaste')?.addEventListener('click', () => {
      saveProfile();
      const button = $('#saveTaste');
      button.textContent = 'Saved';
      window.setTimeout(() => { button.textContent = 'Save my taste'; }, 1400);
    });
  }

  applyThemeControls();
  setupGlobalControls();
  setActiveNav();
  handleGlobalClicks();

  switch (window.ART_CONCIERGE_PAGE) {
    case 'home': initHome(); break;
    case 'dashboard': initDashboard(); break;
    case 'concierge': initConcierge(); break;
    case 'results': initResults(); break;
    case 'saved': initSaved(); break;
    case 'taste': initTaste(); break;
    default: break;
  }
})();

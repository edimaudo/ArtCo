(() => {
  document.documentElement.dataset.theme = localStorage.getItem('artConciergeTheme') || 'light';
  document.documentElement.style.colorScheme = document.documentElement.dataset.theme;
  const savedFontScale = localStorage.getItem('artConciergeFontScale') || 'md';
  document.documentElement.style.setProperty('--scale', ({ sm: 0.92, md: 1, lg: 1.12 }[savedFontScale] || 1));

  const storedProfile = JSON.parse(localStorage.getItem('artConciergeProfile') || '{}');
  const state = {
    view: 'home',
    intent: 'discover',
    loves: Array.isArray(storedProfile.loves) ? storedProfile.loves.slice(0, 12) : [],
    goal: storedProfile.goal || '',
    discoveryLevel: Number(storedProfile.discoveryLevel ?? 50),
    saved: JSON.parse(localStorage.getItem('artConciergeSaved') || '[]'),
    recent: JSON.parse(localStorage.getItem('artConciergeRecent') || '[]'),
    lastRequest: null,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];

  function saveProfile() {
    localStorage.setItem('artConciergeProfile', JSON.stringify({
      loves: state.loves,
      goal: state.goal,
      discoveryLevel: state.discoveryLevel,
    }));
  }

  function persistRecent(request, resultData) {
    const item = {
      id: `${Date.now()}`,
      intent: request.intent,
      prompt: request.goal || intentLabel(request.intent),
      savedAt: new Date().toISOString(),
      data: resultData,
    };
    state.recent = [item, ...state.recent.filter((entry) => entry.prompt !== item.prompt)].slice(0, 6);
    localStorage.setItem('artConciergeRecent', JSON.stringify(state.recent));
    renderHomePanels();
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

  function setView(view) {
    state.view = view;
    $$('.view').forEach((section) => {
      const active = section.dataset.view === view;
      section.hidden = !active;
      section.classList.toggle('active', active);
    });
    window.scrollTo({ top: 0, behavior: 'instant' });
    if (view === 'home') renderHomePanels();
    if (view === 'mytaste') renderTasteProfile();
    if (view === 'saved') renderSavedPage();
  }

  function renderChips() {
    const container = $('#loveChips');
    if (!container) return;
    container.innerHTML = state.loves.map((value, index) => `
      <span class="chip">${escapeHtml(value)} <button type="button" aria-label="Remove ${escapeHtml(value)}" data-index="${index}">×</button></span>`).join('');
  }

  function addLove(value) {
    const clean = value.trim().replace(/,$/, '');
    if (!clean || state.loves.includes(clean) || state.loves.length >= 12) return;
    state.loves.push(clean);
    renderChips();
    $('#loveInput').value = '';
    saveProfile();
  }

  function discoveryLabel(value) {
    if (value < 34) return 'Familiar';
    if (value < 68) return 'Balanced';
    return 'Unexpected';
  }

  function updateRequestFields() {
    const buyingContext = ['buy', 'find', 'curate'].includes(state.intent);
    const countContext = ['buy', 'curate'].includes(state.intent);
    $('#countBlock').hidden = !countContext;
    $('#budgetBlock').hidden = !buyingContext;
    $('#marketBlock').hidden = !buyingContext;
    $('#mediumBlock').hidden = state.intent === 'taste';

    const titles = {
      discover: ['A little direction will help.', 'Tell me what you want to explore, and I’ll do the searching.'],
      find: ['A few details will narrow it down.', 'Tell me enough to understand the brief.'],
      taste: ['Let’s give your taste a little room.', 'I’ll use your references to introduce you to artists and directions you may not know yet.'],
      curate: ['What needs to work together?', 'I’ll look for works that make sense individually and as a group.'],
      buy: ['Tell me what you can act on.', 'I’ll prioritise artwork that is actually available to acquire.'],
      keep_discovering: ['Let’s keep going from here.', 'I’ll build on what you already like instead of starting from scratch.'],
    };
    const [title, lede] = titles[state.intent] || titles.discover;
    $('#preferenceTitle').textContent = title;
    $('#preferenceLede').textContent = lede;
    $('#preferenceContext').textContent = intentLabel(state.intent);
  }

  function setIntent(intent, quickText = '') {
    state.intent = intent;
    $$('.intent-card').forEach((card) => card.classList.toggle('selected', card.dataset.intent === intent));
    updateRequestFields();
    if (quickText) $('#goal').value = quickText;
    setView('taste');
  }

  function inferIntentFromText(text) {
    const lower = text.toLowerCase();
    if (/buy|purchase|acquire|price|budget/.test(lower)) return 'buy';
    if (/curate|collection|set of|several/.test(lower)) return 'curate';
    if (/specific|looking for|need a/.test(lower)) return 'find';
    if (/taste|learn|understand what i like/.test(lower)) return 'taste';
    if (/again|continue|keep/.test(lower)) return 'keep_discovering';
    return 'discover';
  }

  function getChecked(name) {
    return $$(`input[name="${name}"]:checked`).map((el) => el.value);
  }

  function buildPayload() {
    return {
      intent: state.intent,
      loves: [...state.loves],
      additional_interests: [],
      art_interests: getChecked('art'),
      mediums: getChecked('medium'),
      budget_min: null,
      budget_max: $('#budgetMax').value ? Number($('#budgetMax').value) : null,
      room: $('#room').value || null,
      preferred_market: $('#preferredMarket').value || null,
      size_preference: null,
      goal: ($('#goal').value || state.goal || '').trim() || null,
      discovery_level: Number($('#discovery').value || state.discoveryLevel),
      purchase_required: state.intent === 'buy',
      number_of_works: Number($('#numberOfWorks').value || 1),
    };
  }

  function beginQuickRequest() {
    const text = $('#quickPrompt').value.trim();
    if (!text) {
      setIntent('discover');
      return;
    }
    const inferred = inferIntentFromText(text);
    setIntent(inferred, text);
  }

  async function runConcierge(feedback = null) {
    const payload = state.lastRequest ? { ...state.lastRequest } : buildPayload();
    if (feedback) payload.feedback = feedback;
    state.lastRequest = payload;
    saveProfile();
    setView('research');
    $('#researchHeading').textContent = feedback ? 'I’m refining it.' : 'I’m on it.';
    $('#researchMessage').textContent = feedback
      ? 'I’m adjusting the search based on what you told me.'
      : 'I’m looking through the art world for the strongest matches.';
    $('#researchDetail').textContent = 'Searching, comparing and narrowing the field.';

    try {
      const response = await fetch('/api/concierge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const errorPayload = await response.json().catch(() => null);
        throw new Error(errorPayload?.detail?.[0]?.msg || errorPayload?.detail || `Request failed (${response.status})`);
      }
      const data = await response.json();
      storeResultState(data);
      renderResults(data);
      persistRecent(payload, data);
    } catch (error) {
      $('#researchHeading').textContent = 'I couldn’t complete that search.';
      $('#researchMessage').textContent = error.message;
      $('#researchDetail').textContent = 'Check your connected services and try again.';
    }
  }

  function renderTasteSummary(data) {
    const resolved = data.taste?.resolved || [];
    const artists = data.taste?.related_artists || [];
    const inputs = data.taste?.inputs || [];
    const tags = [...inputs.slice(0, 5), ...artists.slice(0, 3).map((artist) => artist.name)].filter(Boolean);
    if (!tags.length) {
      $('#tasteSummary').hidden = true;
      return;
    }
    $('#tasteSummary').hidden = false;
    const resolvedCount = resolved.length;
    $('#tasteSummaryCopy').textContent = resolvedCount
      ? `I used ${resolvedCount} of your cultural references to shape the search.`
      : 'I used the references and preferences you gave me to shape the search.';
    $('#tasteTags').innerHTML = [...new Set(tags)].map((tag) => `<span class="taste-tag">${escapeHtml(tag)}</span>`).join('');
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
    const notForSale = data.not_for_sale || [];
    $('#artGrid').innerHTML = results.length
      ? results.map(renderCard).join('')
      : `<div class="empty-state">${escapeHtml(intent === 'buy' ? 'I could not find purchase inventory in the connected commercial sources.' : 'I could not find strong matches. Try another reference or adjust the brief.')}</div>`;

    $('#notForSaleSection').hidden = !notForSale.length;
    if (notForSale.length) {
      $('#notForSaleGrid').innerHTML = notForSale.map(renderCard).join('');
      $('#notForSaleHint').textContent = intent === 'buy'
        ? 'These works are not currently available to acquire, but they are relevant to what you asked me to find.'
        : 'These works are relevant references from institutional collections.';
    }
    renderTasteSummary(data);
    setView('results');
  }

  function renderCard(work) {
    const isAvailable = work.availability === 'available';
    const availability = isAvailable ? 'AVAILABLE TO ACQUIRE' : 'NOT FOR SALE';
    const price = work.price_label || '';
    const meta = [work.medium, work.dimensions, work.year, price].filter(Boolean).join(' · ');
    const image = work.image_url
      ? `<img src="${escapeAttribute(work.image_url)}" alt="${escapeAttribute(work.title)} by ${escapeAttribute(work.artist)}" loading="lazy">`
      : '<div class="art-placeholder">Image unavailable</div>';
    const linkLabel = isAvailable ? 'View artwork →' : 'See at source →';
    const link = work.detail_url
      ? `<a class="card-action" href="${escapeAttribute(work.detail_url)}" target="_blank" rel="noopener noreferrer">${linkLabel}</a>`
      : '';
    const saved = state.saved.some((item) => item.id === work.id);
    const reasons = (work.why || []).map((reason) => cleanReason(reason)).join(' · ');
    const cardId = escapeAttribute(work.id);
    return `
      <article class="art-card" data-art-id="${cardId}">
        <div class="art-image-wrap">${image}</div>
        <div class="art-body">
          <div class="art-title">${escapeHtml(work.title || 'Untitled')}</div>
          <div class="art-artist">${escapeHtml(work.artist || 'Unknown artist')}</div>
          <div class="art-meta">${escapeHtml(meta || work.source || '')}</div>
          <div class="art-reason">${escapeHtml(reasons)}</div>
          <div class="art-footer"><span class="availability ${work.availability}">${availability}</span><span class="art-source">${escapeHtml(work.source || '')}</span></div>
          <div class="art-actions">
            ${link}
            <button class="card-action save-action" type="button" data-id="${cardId}" aria-pressed="${saved}">${saved ? 'Saved' : 'Save'}</button>
            <button class="card-action feedback-action" type="button" data-feedback="More like ${escapeAttribute(work.artist || work.title)}">More like this</button>
            <button class="card-action feedback-action" type="button" data-feedback="Not for me: ${escapeAttribute(work.artist || work.title)}">Not for me</button>
          </div>
        </div>
      </article>`;
  }

  function cleanReason(reason) {
    return String(reason || '');
  }

  function collectArtworkById(id) {
    const source = document.querySelector(`[data-art-id="${cssEscape(id)}"]`);
    return state.lastResults?.find((item) => item.id === id)
      || state.lastNotForSale?.find((item) => item.id === id)
      || null;
  }

  function toggleSave(id) {
    const artwork = collectArtworkById(id);
    if (!artwork) return;
    const index = state.saved.findIndex((item) => item.id === id);
    if (index >= 0) state.saved.splice(index, 1);
    else state.saved.unshift(artwork);
    localStorage.setItem('artConciergeSaved', JSON.stringify(state.saved.slice(0, 30)));
    renderResults({
      intent: state.lastRequest?.intent || state.intent,
      summary: state.lastResultsSummary || '',
      results: state.lastResults || [],
      not_for_sale: state.lastNotForSale || [],
      purchase_available: state.lastPurchaseAvailable || 0,
      total_found: state.lastTotalFound || 0,
      taste: state.lastTaste || {},
    });
  }

  function renderHomePanels() {
    const recent = $('#recentList');
    if (!state.recent.length) {
      recent.innerHTML = '<div class="empty-state">Your requests will appear here as you work with your concierge.</div>';
    } else {
      recent.innerHTML = state.recent.map((item) => `
        <div class="recent-item">
          <div class="recent-copy">
            <div class="recent-title">${escapeHtml(item.prompt)}</div>
            <div class="recent-meta">${escapeHtml(intentLabel(item.intent))}</div>
          </div>
          <button class="secondary-button recent-open" type="button" data-recent-id="${escapeAttribute(item.id)}">Open</button>
        </div>`).join('');
    }
    $('#recentNote').textContent = state.recent.length ? `${state.recent.length} saved` : '';

    const preview = $('#savedPreview');
    const saved = state.saved.slice(0, 3);
    preview.innerHTML = saved.length
      ? saved.map((work) => work.image_url
        ? `<button class="saved-thumb" type="button" data-saved-id="${escapeAttribute(work.id)}" aria-label="Open ${escapeAttribute(work.title)}"><img src="${escapeAttribute(work.image_url)}" alt=""></button>`
        : `<button class="saved-thumb no-image" type="button" data-saved-id="${escapeAttribute(work.id)}">${escapeHtml(work.title || 'Saved art')}</button>`).join('')
      : '<div class="empty-state">Save works here when you find something you want to revisit.</div>';
  }

  function renderSavedPage() {
    const grid = $('#savedGrid');
    grid.innerHTML = state.saved.length
      ? state.saved.map(renderCard).join('')
      : '<div class="empty-state">Nothing saved yet.</div>';
  }

  function renderTasteProfile() {
    const container = $('#tasteProfileChips');
    const empty = $('#tasteProfileEmpty');
    container.innerHTML = state.loves.map((value, index) => `
      <span class="chip">${escapeHtml(value)} <button type="button" data-profile-index="${index}" aria-label="Remove ${escapeHtml(value)}">×</button></span>`).join('');
    empty.hidden = state.loves.length > 0;
  }

  function loadRecent(id) {
    const item = state.recent.find((entry) => entry.id === id);
    if (!item?.data) return;
    state.lastRequest = {
      intent: item.data.intent || item.intent,
      loves: [...state.loves],
      goal: item.prompt,
      art_interests: [],
      mediums: [],
      budget_min: null,
      budget_max: null,
      room: null,
      preferred_market: null,
      discovery_level: state.discoveryLevel,
      purchase_required: item.intent === 'buy',
      number_of_works: 1,
    };
    state.lastResults = item.data.results || [];
    state.lastNotForSale = item.data.not_for_sale || [];
    state.lastResultsSummary = item.data.summary || '';
    state.lastPurchaseAvailable = item.data.purchase_available || 0;
    state.lastTotalFound = item.data.total_found || 0;
    state.lastTaste = item.data.taste || {};
    renderResults(item.data);
  }

  function cssEscape(value) {
    if (window.CSS?.escape) return window.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }
  function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[char]); }
  function escapeAttribute(value) { return escapeHtml(value); }

  // Intent shortcuts
  $$('.intent-card').forEach((card) => card.addEventListener('click', () => setIntent(card.dataset.intent)));
  $('#quickStart').addEventListener('click', beginQuickRequest);
  $('#quickPrompt').addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') beginQuickRequest();
  });

  // Taste input
  $('#loveInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); addLove(event.target.value); }
  });
  $('#loveInput').addEventListener('blur', (event) => { if (event.target.value.trim()) addLove(event.target.value); });
  $('#loveChips').addEventListener('click', (event) => {
    const index = event.target.dataset.index;
    if (index !== undefined) { state.loves.splice(Number(index), 1); renderChips(); saveProfile(); }
  });
  $('#tasteContinue').addEventListener('click', () => {
    state.goal = $('#goal').value.trim();
    saveProfile();
    setView('preferences');
  });

  // Preferences
  $('#discovery').value = String(state.discoveryLevel);
  $('#discoveryValue').textContent = discoveryLabel(state.discoveryLevel);
  $('#discovery').addEventListener('input', (event) => {
    state.discoveryLevel = Number(event.target.value);
    $('#discoveryValue').textContent = discoveryLabel(state.discoveryLevel);
    saveProfile();
  });
  $('#researchButton').addEventListener('click', () => {
    state.lastRequest = buildPayload();
    runConcierge();
  });

  // Results
  $('#resultsView')?.addEventListener('click', () => {});
  document.body.addEventListener('click', (event) => {
    const save = event.target.closest('.save-action');
    if (save) toggleSave(save.dataset.id);

    const feedback = event.target.closest('.feedback-action');
    if (feedback && state.lastRequest) runConcierge(feedback.dataset.feedback);

    const recent = event.target.closest('.recent-open');
    if (recent) loadRecent(recent.dataset.recentId);

    const savedThumb = event.target.closest('.saved-thumb');
    if (savedThumb) {
      const artwork = state.saved.find((item) => item.id === savedThumb.dataset.savedId);
      if (artwork) {
        state.lastResults = [artwork];
        state.lastNotForSale = [];
        state.lastResultsSummary = 'Saved from an earlier conversation with your concierge.';
        state.lastRequest = { intent: 'find', loves: state.loves, goal: artwork.title, art_interests: [], mediums: [], budget_min: null, budget_max: null, room: null, preferred_market: null, discovery_level: state.discoveryLevel, purchase_required: false, number_of_works: 1 };
        renderResults({ intent: 'find', summary: state.lastResultsSummary, results: [artwork], not_for_sale: [], purchase_available: artwork.availability === 'available' ? 1 : 0, total_found: 1, taste: { inputs: state.loves } });
      }
    }

    const profileRemove = event.target.closest('[data-profile-index]');
    if (profileRemove) {
      state.loves.splice(Number(profileRemove.dataset.profileIndex), 1);
      saveProfile();
      renderTasteProfile();
    }
  });

  $('#feedback').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      const value = $('#feedback').value.trim();
      if (value) { $('#feedback').value = ''; runConcierge(value); }
    }
  });
  $('#refineButton').addEventListener('click', () => {
    const value = $('#feedback').value.trim();
    if (value) { $('#feedback').value = ''; runConcierge(value); }
  });
  $('#newRequest').addEventListener('click', () => setView('home'));

  // Navigation
  $$('[data-go]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.go)));
  $$('[data-nav="home"]').forEach((link) => link.addEventListener('click', (event) => { event.preventDefault(); setView('home'); }));
  $('#savedNav').addEventListener('click', () => setView('saved'));
  $('#tasteNav').addEventListener('click', () => setView('mytaste'));
  $('#editTaste').addEventListener('click', () => { setIntent('taste'); $('#goal').value = state.goal; renderChips(); });
  $('#useSavedTaste').addEventListener('click', () => { renderChips(); $('#goal').value = state.goal; });

  // Theme and text controls
  $('#themeToggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme || 'light';
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    localStorage.setItem('artConciergeTheme', next);
  });
  $('#fontScale').value = savedFontScale;
  $('#fontScale').addEventListener('change', (event) => {
    const scales = { sm: 0.92, md: 1, lg: 1.12 };
    document.documentElement.style.setProperty('--scale', scales[event.target.value]);
    localStorage.setItem('artConciergeFontScale', event.target.value);
  });

  function storeResultState(data) {
    state.lastResults = data.results || [];
    state.lastNotForSale = data.not_for_sale || [];
    state.lastResultsSummary = data.summary || '';
    state.lastPurchaseAvailable = data.purchase_available || 0;
    state.lastTotalFound = data.total_found || 0;
    state.lastTaste = data.taste || {};
  }

  renderChips();
  renderHomePanels();
})();

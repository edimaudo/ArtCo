(() => {
  const storedTheme = localStorage.getItem('artConciergeTheme') || 'light';
  const storedScale = localStorage.getItem('artConciergeFontScale') || 'md';
  document.documentElement.dataset.theme = storedTheme;
  document.documentElement.style.colorScheme = storedTheme;
  const scales = { sm: 0.92, md: 1, lg: 1.12 };
  document.documentElement.style.setProperty('--scale', scales[storedScale] || 1);

  const state = {
    intent: 'discover',
    loves: [],
    discoveryLevel: 50,
    lastRequest: null,
    saved: JSON.parse(localStorage.getItem('artConciergeSaved') || '[]'),
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];

  const intentCopy = {
    discover: 'I’ll explore beyond the most obvious matches and bring back a focused set of discoveries.',
    find: 'I’ll translate your brief into search criteria and narrow the field to the strongest fits.',
    taste: 'I’ll use your references to give you useful places to start and new directions to explore.',
    curate: 'I’ll look for works that make sense individually and alongside one another.',
    buy: 'I’ll prioritise artwork that is currently marked as available to acquire, then show relevant works that are not for sale separately.',
    keep_discovering: 'I’ll build on your current taste and keep the next search meaningfully adjacent rather than repetitive.',
  };

  function showView(name) {
    $$('.view').forEach((view) => {
      const active = view.dataset.view === name;
      view.hidden = !active;
      view.classList.toggle('active', active);
    });
  }

  function setIntent(intent) {
    state.intent = intent;
    $$('.intent-card').forEach((card) => card.classList.toggle('selected', card.dataset.intent === intent));
    updateDetailsForIntent();
  }

  function updateDetailsForIntent() {
    const commercial = ['find', 'curate', 'buy'].includes(state.intent);
    $$('.commercial-only').forEach((el) => { el.hidden = !commercial; });
    $('#detailsIntro').textContent = intentCopy[state.intent];
    $('#artInterestField').hidden = state.intent === 'taste';
  }

  function discoveryLabel(value) {
    if (value < 34) return 'Close to my taste';
    if (value < 68) return 'Balanced';
    return 'Surprising';
  }

  function renderReferences() {
    const container = $('#loveChips');
    container.innerHTML = state.loves.map((value, index) => `
      <span class="reference-token">
        ${escapeHtml(value)}
        <button type="button" data-index="${index}" aria-label="Remove ${escapeAttribute(value)}">×</button>
      </span>`).join('');
  }

  function addReference(value) {
    const clean = value.trim().replace(/,$/, '');
    if (!clean || state.loves.includes(clean) || state.loves.length >= 12) return;
    state.loves.push(clean);
    renderReferences();
    $('#loveInput').value = '';
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
      preferred_market: $('#preferredMarket').value.trim() || null,
      size_preference: null,
      goal: $('#goal').value.trim() || null,
      discovery_level: Number($('#discovery').value),
      purchase_required: state.intent === 'buy',
      number_of_works: Number($('#numberOfWorks').value || 1),
    };
  }

  function buildReview() {
    const payload = buildPayload();
    const references = payload.loves.length ? payload.loves.join(', ') : 'No references added yet';
    const interests = payload.art_interests.length ? payload.art_interests.join(', ') : 'Open to ideas';
    const practical = [];
    if (payload.budget_max != null) practical.push(`up to $${payload.budget_max.toLocaleString()}`);
    if (payload.number_of_works) practical.push(`${payload.number_of_works} work${payload.number_of_works === 1 ? '' : 's'}`);
    if (payload.room) practical.push(payload.room);
    if (payload.preferred_market) practical.push(payload.preferred_market);
    if (payload.mediums.length) practical.push(payload.mediums.join(', '));

    $('#reviewCard').innerHTML = [
      ['Looking for', state.intent.replace('_', ' ')],
      ['You like', references],
      ['Art interests', interests],
      ['Exploration', discoveryLabel(payload.discovery_level)],
      ['Context', practical.length ? practical.join(' · ') : 'No additional constraints'],
      ['Brief', payload.goal || 'No additional note'],
    ].map(([label, value]) => `
      <div class="review-row">
        <div class="review-label">${escapeHtml(label)}</div>
        <div class="review-value">${escapeHtml(value)}</div>
      </div>`).join('');

    state.lastRequest = payload;
  }

  function renderResearch(data) {
    const items = data.status || [];
    $('#statusList').innerHTML = items.map((item) => `
      <div class="activity-item ${escapeAttribute(item.state || '')}">
        <span class="activity-dot" aria-hidden="true"></span>
        <span>${escapeHtml(item.label)}</span>
      </div>`).join('');
  }

  function renderTasteSummary(data) {
    const inputs = data.taste?.inputs || [];
    const artists = data.taste?.related_artists || [];
    const tags = [...inputs.slice(0, 5), ...artists.slice(0, 3).map((item) => item.name)].filter(Boolean);
    if (!tags.length) {
      $('#tasteSummary').hidden = true;
      return;
    }
    $('#tasteSummary').hidden = false;
    $('#tasteSummaryCopy').textContent = 'The shortlist is built from the cultural references and preferences you gave me.';
    $('#tasteTags').innerHTML = [...new Set(tags)].map((tag) => `<span class="reference-token">${escapeHtml(tag)}</span>`).join('');
  }

  function renderResults(data) {
    const intent = data.intent || state.intent;
    const headings = {
      discover: 'Your discoveries',
      find: 'Your closest matches',
      taste: 'A place to start',
      curate: 'Your shortlist',
      buy: 'Available to acquire',
      keep_discovering: 'Your next discoveries',
    };
    $('#results-title').textContent = headings[intent] || 'Your shortlist';
    $('#resultsSummary').textContent = data.summary || '';

    const results = data.results || [];
    const notForSale = data.not_for_sale || [];
    $('#artGrid').innerHTML = results.length
      ? results.map(renderCard).join('')
      : `<div class="empty-state">I couldn't find a strong match from the connected sources. Try another reference or adjust the brief.</div>`;

    $('#notForSaleSection').hidden = !notForSale.length;
    if (notForSale.length) {
      $('#notForSaleGrid').innerHTML = notForSale.map(renderCard).join('');
      $('#notForSaleHint').textContent = intent === 'buy'
        ? 'These works are relevant to your taste but are not currently marked as available to acquire. You can still see them at their source.'
        : 'These works are not currently marked as available to acquire. They are included because they are relevant to your search.';
    }
  }

  function renderCard(work) {
    const isAvailable = work.availability === 'available' && work.source_kind === 'commercial';
    const availability = isAvailable ? 'Available to acquire' : 'Not for sale';
    const meta = [work.medium, work.dimensions, work.year, work.price_label].filter(Boolean).join(' · ');
    const image = work.image_url
      ? `<img src="${escapeAttribute(work.image_url)}" alt="${escapeAttribute(work.title)} by ${escapeAttribute(work.artist)}" loading="lazy">`
      : '<div class="art-placeholder">Image unavailable</div>';
    const linkLabel = isAvailable ? 'View listing' : 'See at source';
    const link = work.detail_url
      ? `<a class="card-action primary-link" href="${escapeAttribute(work.detail_url)}" target="_blank" rel="noopener noreferrer">${linkLabel}</a>`
      : '';
    const saved = state.saved.includes(work.id);
    const reasons = (work.why || []).map((reason) => escapeHtml(reason)).join(' · ');
    const id = escapeAttribute(work.id);
    return `
      <article class="art-card">
        <div class="art-image-wrap">${image}</div>
        <div class="art-body">
          <div class="art-title">${escapeHtml(work.title || 'Untitled')}</div>
          <div class="art-artist">${escapeHtml(work.artist || 'Unknown artist')}</div>
          <div class="art-meta">${escapeHtml(meta || work.source || '')}</div>
          ${reasons ? `<div class="art-reason">${reasons}</div>` : ''}
          <div class="art-status">${availability}</div>
          <div class="art-actions">
            ${link}
            <button class="card-action save-action" type="button" data-id="${id}" aria-pressed="${saved}">${saved ? 'Saved' : 'Save'}</button>
            <button class="card-action feedback-action" type="button" data-feedback="More like ${escapeAttribute(work.artist || work.title)}">More like this</button>
            <button class="card-action feedback-action" type="button" data-feedback="Not for me: ${escapeAttribute(work.artist || work.title)}">Not for me</button>
          </div>
        </div>
      </article>`;
  }

  async function runConcierge(feedback = null) {
    const payload = feedback ? { ...state.lastRequest, feedback } : { ...state.lastRequest };
    showView('research');
    $('#statusList').innerHTML = '<div class="activity-item active"><span class="activity-dot"></span><span>Preparing the search</span></div>';
    $('#runConcierge').disabled = true;

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
      renderResearch(data);
      renderTasteSummary(data);
      renderResults(data);
      showView('results');
    } catch (error) {
      $('#statusList').innerHTML = `<div class="activity-item"><span class="activity-dot"></span><span>${escapeHtml(error.message)}. Check your API credentials and source connectivity.</span></div>`;
    } finally {
      $('#runConcierge').disabled = false;
    }
  }

  function toggleSave(id) {
    if (state.saved.includes(id)) state.saved = state.saved.filter((item) => item !== id);
    else state.saved.push(id);
    localStorage.setItem('artConciergeSaved', JSON.stringify(state.saved));
    const button = $(`.save-action[data-id="${cssEscape(id)}"]`);
    if (button) {
      const saved = state.saved.includes(id);
      button.textContent = saved ? 'Saved' : 'Save';
      button.setAttribute('aria-pressed', String(saved));
    }
  }

  function cssEscape(value) {
    if (window.CSS?.escape) return window.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }

  function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[char]); }
  function escapeAttribute(value) { return escapeHtml(value); }

  $$('.intent-card').forEach((card) => card.addEventListener('click', () => setIntent(card.dataset.intent)));
  $('#startBrief').addEventListener('click', () => showView('taste'));
  $$('[data-next]').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.next === 'review') buildReview();
    showView(button.dataset.next);
  }));
  $$('[data-back]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.back)));

  $('#loveInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); addReference(event.target.value); }
  });
  $('#loveInput').addEventListener('blur', (event) => { if (event.target.value.trim()) addReference(event.target.value); });
  $('#loveChips').addEventListener('click', (event) => {
    const index = event.target.dataset.index;
    if (index !== undefined) { state.loves.splice(Number(index), 1); renderReferences(); }
  });
  $('#discovery').addEventListener('input', (event) => {
    state.discoveryLevel = Number(event.target.value);
    $('#discoveryValue').textContent = discoveryLabel(state.discoveryLevel);
  });
  $('#runConcierge').addEventListener('click', () => { state.lastRequest = buildPayload(); runConcierge(); });
  $('#results').addEventListener('click', (event) => {
    const save = event.target.closest('.save-action');
    if (save) toggleSave(save.dataset.id);
    const feedback = event.target.closest('.feedback-action');
    if (feedback) runConcierge(feedback.dataset.feedback);
  });
  $('#refineButton').addEventListener('click', () => {
    const feedback = $('#feedback').value.trim();
    if (!feedback) return;
    $('#feedback').value = '';
    runConcierge(feedback);
  });
  $('#newSearch').addEventListener('click', () => showView('intro'));

  $('#themeToggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme || 'light';
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    $('#themeToggle').textContent = next === 'dark' ? 'Light' : 'Dark';
    localStorage.setItem('artConciergeTheme', next);
  });
  $('#themeToggle').textContent = storedTheme === 'dark' ? 'Light' : 'Dark';

  $('#fontScale').value = storedScale;
  $('#fontScale').addEventListener('change', (event) => {
    document.documentElement.style.setProperty('--scale', scales[event.target.value]);
    localStorage.setItem('artConciergeFontScale', event.target.value);
  });

  setIntent('discover');
})();

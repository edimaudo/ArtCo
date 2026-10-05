(() => {
  document.documentElement.dataset.theme = localStorage.getItem('artConciergeTheme') || 'light';
  document.documentElement.style.colorScheme = document.documentElement.dataset.theme;

  const state = {
    intent: 'discover',
    loves: [],
    discoveryLevel: 50,
    lastRequest: null,
    saved: JSON.parse(localStorage.getItem('artConciergeSaved') || '[]'),
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];

  function renderChips() {
    const container = $('#loveChips');
    container.innerHTML = '';
    state.loves.forEach((value, index) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = `${escapeHtml(value)} <button type="button" aria-label="Remove ${escapeHtml(value)}" data-index="${index}">×</button>`;
      container.appendChild(chip);
    });
  }

  function addLove(value) {
    const clean = value.trim().replace(/,$/, '');
    if (!clean || state.loves.includes(clean) || state.loves.length >= 12) return;
    state.loves.push(clean);
    renderChips();
    $('#loveInput').value = '';
  }

  function discoveryLabel(value) {
    if (value < 34) return 'Familiar';
    if (value < 68) return 'Balanced';
    return 'Unexpected';
  }

  function updateConditionalFields() {
    const buyingContext = ['buy', 'find', 'curate'].includes(state.intent);
    $('#purchaseFields').hidden = !buyingContext;
    $('#marketFields').hidden = !buyingContext;
    $('#mediumFields').hidden = state.intent === 'taste';
  }

  function setIntent(intent) {
    state.intent = intent;
    $$('.intent-card').forEach((card) => card.classList.toggle('selected', card.dataset.intent === intent));
    updateConditionalFields();
  }

  function getChecked(name) {
    return $$(`input[name="${name}"]:checked`).map((el) => el.value);
  }

  function buildPayload() {
    const min = $('#budgetMin').value ? Number($('#budgetMin').value) : null;
    const max = $('#budgetMax').value ? Number($('#budgetMax').value) : null;
    return {
      intent: state.intent,
      loves: [...state.loves],
      additional_interests: [],
      art_interests: getChecked('art'),
      mediums: getChecked('medium'),
      budget_min: min,
      budget_max: max,
      room: $('#room').value || null,
      preferred_market: $('#preferredMarket').value || null,
      size_preference: null,
      goal: $('#goal').value.trim() || null,
      discovery_level: Number($('#discovery').value),
      purchase_required: state.intent === 'buy',
    };
  }

  async function runConcierge(feedback = null) {
    const payload = state.lastRequest ? { ...state.lastRequest } : buildPayload();
    if (!state.lastRequest) state.lastRequest = payload;
    if (feedback) {
      payload.feedback = feedback;
      state.lastRequest = payload;
    }

    $('#research').hidden = false;
    $('#results').hidden = true;
    $('#statusList').innerHTML = '';
    $('#searchDirections').innerHTML = '';
    $('#researchCount').textContent = 'Researching';
    $('#research').scrollIntoView({ behavior: 'smooth', block: 'start' });

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
    } catch (error) {
      $('#statusList').innerHTML = `<div class="status-row"><span class="status-dot"></span><span>${escapeHtml(error.message)}. Check your API credentials and source connectivity.</span></div>`;
      $('#researchCount').textContent = 'Unable to complete the search';
    }
  }

  function renderResearch(data) {
    const list = $('#statusList');
    list.innerHTML = (data.status || []).map((item) => `
      <div class="status-row ${item.state}">
        <span class="status-dot" aria-hidden="true"></span>
        <span>${escapeHtml(item.label)}</span>
      </div>`).join('');

    $('#researchCount').textContent = `${data.total_found || 0} artworks researched`;
    $('#searchDirections').innerHTML = (data.search_directions || []).map((direction) => `<span class="direction">${escapeHtml(direction)}</span>`).join('');
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
      ? `I connected ${resolvedCount} of your cultural references to the taste signals used for the artwork search.`
      : 'I used the cultural references and art preferences in your brief to shape the artwork search.';
    $('#tasteTags').innerHTML = [...new Set(tags)].map((tag) => `<span class="taste-tag">${escapeHtml(tag)}</span>`).join('');
  }

  function renderResults(data) {
    $('#results').hidden = false;
    $('#results').scrollIntoView({ behavior: 'smooth', block: 'start' });
    const results = data.results || [];
    const available = data.purchase_available || 0;
    $('#resultsMeta').textContent = available
      ? `${available} available to acquire · ${data.total_found || 0} researched`
      : `${data.total_found || 0} researched across cultural collections`;
    $('#artGrid').innerHTML = results.length
      ? results.map(renderCard).join('')
      : '<div class="empty-state">No strong matches came back from the connected sources. Try adding another cultural reference or broadening your discovery level.</div>';

    const notForSale = data.not_for_sale || [];
    $('#notForSaleSection').hidden = !notForSale.length;
    if (notForSale.length) $('#notForSaleGrid').innerHTML = notForSale.map(renderCard).join('');
  }

  function renderCard(work) {
    const isAvailable = work.availability === 'available';
    const availability = isAvailable ? 'AVAILABLE TO ACQUIRE' : 'NOT FOR SALE';
    const price = work.price != null ? `${work.currency || 'USD'} ${Number(work.price).toLocaleString()}` : '';
    const meta = [work.medium, work.dimensions, work.year, price].filter(Boolean).join(' · ');
    const image = work.image_url
      ? `<img src="${escapeAttribute(work.image_url)}" alt="${escapeAttribute(work.title)} by ${escapeAttribute(work.artist)}" loading="lazy">`
      : '<div class="art-placeholder">Image unavailable</div>';
    const linkLabel = isAvailable ? 'View listing →' : 'View source →';
    const link = work.detail_url
      ? `<a class="card-action" href="${escapeAttribute(work.detail_url)}" target="_blank" rel="noopener noreferrer">${linkLabel}</a>`
      : '';
    const saved = state.saved.includes(work.id);
    const reasons = (work.why || []).map((reason) => escapeHtml(reason)).join(' · ');
    const cardId = escapeAttribute(work.id);
    return `
      <article class="art-card" data-art-id="${cardId}">
        <div class="art-image-wrap">${image}</div>
        <div class="art-body">
          <div class="art-title">${escapeHtml(work.title || 'Untitled')}</div>
          <div class="art-artist">${escapeHtml(work.artist || 'Unknown artist')}</div>
          <div class="art-meta">${escapeHtml(meta || work.source || '')}</div>
          <div class="art-reason">${reasons}</div>
          <div class="art-footer"><span class="availability ${work.availability}">${availability}</span><span class="art-source">${escapeHtml(work.source || '')}</span></div>
          <div class="art-actions">
            ${link}
            <button class="card-action save-action" type="button" data-id="${cardId}" data-title="${escapeAttribute(work.title)}" aria-pressed="${saved}">${saved ? 'Saved' : 'Save'}</button>
            <button class="card-action feedback-action" type="button" data-feedback="More like ${escapeAttribute(work.artist || work.title)}">More like this</button>
          </div>
        </div>
      </article>`;
  }

  function toggleSave(id, title) {
    if (state.saved.includes(id)) {
      state.saved = state.saved.filter((item) => item !== id);
    } else {
      state.saved.push(id);
    }
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

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[char]);
  }
  function escapeAttribute(value) { return escapeHtml(value); }

  $$('.intent-card').forEach((card) => card.addEventListener('click', () => setIntent(card.dataset.intent)));
  $('#loveInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); addLove(event.target.value); }
  });
  $('#loveInput').addEventListener('blur', (event) => { if (event.target.value.trim()) addLove(event.target.value); });
  $('#loveChips').addEventListener('click', (event) => {
    const index = event.target.dataset.index;
    if (index !== undefined) { state.loves.splice(Number(index), 1); renderChips(); }
  });
  $('#discovery').addEventListener('input', (event) => {
    state.discoveryLevel = Number(event.target.value);
    $('#discoveryValue').textContent = discoveryLabel(state.discoveryLevel);
  });
  $('#runConcierge').addEventListener('click', () => {
    state.lastRequest = buildPayload();
    runConcierge();
  });
  $('#results').addEventListener('click', (event) => {
    const save = event.target.closest('.save-action');
    if (save) toggleSave(save.dataset.id, save.dataset.title);
    const feedback = event.target.closest('.feedback-action');
    if (feedback) runConcierge(feedback.dataset.feedback);
  });
  $('#refineButton').addEventListener('click', () => {
    const feedback = $('#feedback').value.trim();
    if (feedback) runConcierge(feedback);
  });

  $('#themeToggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme || 'light';
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    localStorage.setItem('artConciergeTheme', next);
  });
  $('#fontScale').addEventListener('change', (event) => {
    const scales = { sm: 0.92, md: 1, lg: 1.12 };
    document.documentElement.style.setProperty('--scale', scales[event.target.value]);
  });

  setIntent('discover');
})();

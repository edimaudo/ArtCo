(() => {
  'use strict';

  // Shares the exact IndexedDB database and settings record used by app.js.
  const DB_NAME = 'styletv-db';
  const DB_VERSION = 1;
  const SETTINGS_KEY = 'display-settings';
  const defaults = { theme: 'light', fontSize: 'medium' };
  let settings = { ...defaults };

  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];

  function apply() {
    document.documentElement.dataset.theme = settings.theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.fontSize = ['small', 'medium', 'large'].includes(settings.fontSize) ? settings.fontSize : 'medium';
    $$('[data-font]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.font === document.documentElement.dataset.fontSize));
    });
    const dark = document.documentElement.dataset.theme === 'dark';
    const toggle = $('.theme-toggle');
    if (toggle) {
      toggle.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
      toggle.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
      const label = $('.theme-label', toggle);
      if (label) label.textContent = dark ? 'Light mode' : 'Dark mode';
    }
  }

  function openDatabase() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) return reject(new Error('IndexedDB is not available.'));
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('profile')) db.createObjectStore('profile', { keyPath: 'key' });
        if (!db.objectStoreNames.contains('settings')) db.createObjectStore('settings', { keyPath: 'key' });
        if (!db.objectStoreNames.contains('saved')) db.createObjectStore('saved', { keyPath: 'productId' });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('Could not open display settings.'));
    });
  }

  async function readSavedSettings(db) {
    return new Promise((resolve, reject) => {
      const request = db.transaction('settings', 'readonly').objectStore('settings').get(SETTINGS_KEY);
      request.onsuccess = () => resolve(request.result?.value || null);
      request.onerror = () => reject(request.error || new Error('Could not read display settings.'));
    });
  }

  async function persist(db) {
    if (!db) return;
    return new Promise((resolve, reject) => {
      const request = db.transaction('settings', 'readwrite').objectStore('settings').put({ key: SETTINGS_KEY, value: settings });
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error || new Error('Could not save display settings.'));
    });
  }

  async function init() {
    let db = null;
    try {
      db = await openDatabase();
      const stored = await readSavedSettings(db);
      if (stored) settings = { ...defaults, ...stored };
    } catch (_) {
      // Keep the controls usable for this page view when IndexedDB is unavailable.
    }
    apply();

    $$('[data-font]').forEach(button => button.addEventListener('click', async () => {
      settings.fontSize = button.dataset.font;
      apply();
      try { await persist(db); } catch (_) { /* current page setting still applies */ }
    }));

    $('.theme-toggle')?.addEventListener('click', async () => {
      settings.theme = settings.theme === 'dark' ? 'light' : 'dark';
      apply();
      try { await persist(db); } catch (_) { /* current page setting still applies */ }
    });
  }

  document.addEventListener('DOMContentLoaded', init, { once: true });
})();

// Paprika Bridge -- background service worker (auto-sync).
//
// DEFAULT ("auto") mode: keep the Paprika host registry fresh with the
// browser's live login state so the worker fleet always has current
// cookies WITHOUT any manual push. This is the "set and forget" path:
// you log into a site, and a moment later its cookies are in the hub's
// /hosts/{host} registry, ready for the next job.
//
// When the operator turns ON Developer mode in the popup, auto-sync is
// suspended and cookie registration becomes fully manual via the table
// editor (editor.html).
//
// Triggers (active-tab host ONLY, debounced) -> push that host:
//   * a tab finishes loading      (login redirect / navigation)
//   * a cookie for the active host changes  (XHR / SPA logins)
//
// Privacy: only the host of the tab you are actually looking at is ever
// pushed. We never sweep every site the browser has cookies for. The
// optional "registered hosts only" setting tightens this further to
// "refresh hosts already in the registry, never auto-create new ones".

importScripts('lib.js');

const DEBOUNCE_MS = 2500;
const _timers = new Map();   // host -> timeout id
const _lastFp = new Map();   // host -> last pushed cookie fingerprint
let _activeHost = '';
let _activeUrl = '';

// Settings cache, refreshed via storage.onChanged so the hot cookie
// listener never touches storage on its own.
let _settings = { hubUrl: '', devMode: false, registeredOnly: false, includeSession: true };
const _ready = PB.getSettings().then((s) => { _settings = s; });

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  PB.getSettings().then((s) => { _settings = s; });
});

function autoActive() {
  // Auto-push only when a hub is configured AND dev mode is OFF.
  return !!_settings.hubUrl && !_settings.devMode;
}

// Core auto-push for one host. ``force`` (manual button) bypasses the
// dedup + registered-only guards.
async function autoPush(host, { url = '', force = false } = {}) {
  const hubUrl = _settings.hubUrl;
  if (!hubUrl || !host) return { ok: false, reason: 'no hub/host' };

  const chromeCookies = url
    ? await PB.cookiesForUrl(url, { includeSession: _settings.includeSession })
    : await PB.cookiesForHost(host, { includeSession: _settings.includeSession });
  const cookies = chromeCookies.map(PB.toPaprikaCookie);
  const fp = PB.cookieFingerprint(cookies);
  if (!force && _lastFp.get(host) === fp) return { ok: true, skipped: 'unchanged' };

  const existing = await PB.fetchHost(hubUrl, host);
  if (existing === undefined) return { ok: false, reason: 'hub unreachable' };
  // "registered only": in auto mode, optionally refresh only hosts the
  // hub already knows -- don't spam the registry with every site browsed.
  if (!force && _settings.registeredOnly && existing === null) {
    return { ok: true, skipped: 'not-registered' };
  }
  // Nothing to create from an empty jar (but DO allow clearing an
  // existing record's cookies if they genuinely went away).
  if (!cookies.length && existing === null) return { ok: true, skipped: 'no-cookies' };

  await PB.putHost(hubUrl, host, cookies, existing);
  _lastFp.set(host, fp);
  await PB.setStorage({ ['paprika.lastsync.' + host]: { at: Date.now(), n: cookies.length } });
  return { ok: true, pushed: cookies.length, created: existing === null };
}

function schedule(host, url) {
  if (!host) return;
  if (_timers.has(host)) clearTimeout(_timers.get(host));
  _timers.set(host, setTimeout(async () => {
    _timers.delete(host);
    await _ready;
    if (!autoActive()) return;
    try {
      await autoPush(host, { url });
    } catch (e) {
      console.warn('[paprika-bridge] auto push failed', host, e);
    }
  }, DEBOUNCE_MS));
}

// ---- active-host tracking (keeps the cookie listener cheap) ---------

async function refreshActiveTab() {
  const t = await PB.activeTab();
  _activeHost = t ? PB.hostFromUrl(t.url) : '';
  _activeUrl = (t && PB.isHttpUrl(t.url)) ? t.url : '';
}
chrome.tabs.onActivated.addListener(refreshActiveTab);
chrome.windows.onFocusChanged.addListener(refreshActiveTab);
refreshActiveTab();

// ---- triggers ------------------------------------------------------

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status !== 'complete' || !tab || !PB.isHttpUrl(tab.url)) return;
  const host = PB.hostFromUrl(tab.url);
  if (tab.active) { _activeHost = host; _activeUrl = tab.url; }
  if (!autoActive()) return;
  schedule(host, tab.url);
});

chrome.cookies.onChanged.addListener((info) => {
  if (!autoActive() || !_activeHost) return;
  const dom = PB.normaliseHost((info.cookie.domain || '').replace(/^\./, ''));
  if (!dom) return;
  // React only to changes for the host the operator is currently on.
  if (dom === _activeHost
      || _activeHost.endsWith('.' + dom)
      || dom.endsWith('.' + _activeHost)) {
    schedule(_activeHost, _activeUrl);
  }
});

// ---- popup / editor messages ---------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    await _ready;
    try {
      if (msg && msg.type === 'pushHost') {
        const host = PB.normaliseHost(msg.host || '');
        _lastFp.delete(host);
        sendResponse(await autoPush(host, { url: msg.url || '', force: true }));
      } else if (msg && msg.type === 'pushAll') {
        const res = await PB.pushAllHosts(_settings.hubUrl, { includeSession: _settings.includeSession });
        sendResponse({ ok: true, ...res });
      } else if (msg && msg.type === 'ping') {
        sendResponse({ ok: true, settings: _settings, activeHost: _activeHost });
      } else {
        sendResponse({ ok: false, reason: 'unknown message' });
      }
    } catch (e) {
      sendResponse({ ok: false, reason: String((e && e.message) || e) });
    }
  })();
  return true; // async sendResponse
});

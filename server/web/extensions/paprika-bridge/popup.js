// Paprika Bridge -- popup logic.
//
// The popup is the CONTROL surface; the heavy lifting lives elsewhere:
//   * background.js  runs the auto-sync (default mode)
//   * editor.html    is the table editor (developer mode)
//   * lib.js (PB.*)  holds the shared cookie/host/hub helpers
//
// Here we just: edit settings (hub URL + dev-mode toggle in the ⚙
// menu), show which mode is active, and offer the one mode-appropriate
// action (auto: "sync this site now" / dev: "edit cookies in a table").

const $ = (id) => document.getElementById(id);

let activeHost = '';
let activeUrl = '';

function setStatus(msg, kind = 'info') {
  const el = $('status');
  el.className = kind;
  el.textContent = msg;
}
function clearStatus() {
  const el = $('status');
  el.className = '';
  el.textContent = '';
  el.style.display = 'none';
}

// Render the auto/dev cards + settings checkboxes from current storage.
async function render() {
  const s = await PB.getSettings();
  $('hub').value = s.hubUrlRaw || s.hubUrl;
  $('devMode').checked = s.devMode;
  $('registeredOnly').checked = s.registeredOnly;
  $('includeSession').checked = s.includeSession;

  $('autoCard').style.display = s.devMode ? 'none' : '';
  $('devCard').style.display = s.devMode ? '' : 'none';

  // last-sync hint for the active host (auto mode only).
  if (!s.devMode && activeHost) {
    const st = await PB.getStorage(['paprika.lastsync.' + activeHost]);
    const info = st['paprika.lastsync.' + activeHost];
    if (info && info.at) {
      const ago = Math.round((Date.now() - info.at) / 1000);
      const when = ago < 60 ? `${ago}秒前` : `${Math.round(ago / 60)}分前`;
      $('lastSync').textContent = `最終同期: ${when}（${info.n} cookie）`;
    } else {
      $('lastSync').textContent = s.hubUrl
        ? 'このサイトはまだ同期されていません。ログイン後にページを再読み込みするか「今すぐ登録」。'
        : '⚙ から Hub URL を設定してください。';
    }
  }
}

// ---- settings persistence -------------------------------------------

function saveHub() {
  PB.setStorage({ [PB.STORAGE.HUB]: PB.normaliseHubUrl($('hub').value) });
}
function bindToggle(id, key, afterFn) {
  $(id).addEventListener('change', async () => {
    await PB.setStorage({ [key]: $(id).checked });
    if (afterFn) await afterFn();
  });
}

// ---- actions ---------------------------------------------------------

// Ask the background worker to push the active host now (force). Falls
// back to a direct push if the service worker doesn't answer.
async function syncNow() {
  clearStatus();
  const s = await PB.getSettings();
  if (!s.hubUrl) { setStatus('⚙ から Hub URL を設定してください', 'err'); return; }
  if (!activeHost) { setStatus('このタブのホストを取得できません（chrome:// ページ?）', 'err'); return; }
  const btn = $('syncNow');
  btn.disabled = true;
  setStatus(`${activeHost} を登録中 ...`, 'info');
  try {
    const res = await sendBg({ type: 'pushHost', host: activeHost, url: activeUrl });
    if (res && res.ok && res.pushed != null) {
      setStatus(`✓ ${activeHost} を登録しました（${res.pushed} cookie）${res.created ? ' [新規]' : ''}`, 'ok');
    } else if (res && res.ok && res.skipped === 'no-cookies') {
      setStatus(`${activeHost} に送信できる cookie がありません（ログインしていない / 別プロファイル？）`, 'info');
    } else {
      // service worker asleep or errored -> direct push from the popup itself
      const r = await PB.pushSingleHost(s.hubUrl, activeHost, { url: activeUrl, includeSession: s.includeSession });
      if (r.count === 0) {
        setStatus(`${r.host} に送信できる cookie がありません`, 'info');
      } else {
        setStatus(`✓ ${r.host} を登録しました（${r.count} cookie）${r.created ? ' [新規]' : ''}`, 'ok');
      }
    }
    await render();
  } catch (e) {
    setStatus('登録失敗: ' + (e.message || e), 'err');
  } finally {
    btn.disabled = false;
  }
}

async function pushAll() {
  clearStatus();
  const s = await PB.getSettings();
  if (!s.hubUrl) { setStatus('⚙ から Hub URL を設定してください', 'err'); return; }
  const btn = $('pushAll');
  btn.disabled = true;
  setStatus('全ホストの cookie を読み込み・登録中 ...', 'info');
  try {
    const r = await PB.pushAllHosts(s.hubUrl, { includeSession: s.includeSession });
    if (r.failed === 0) {
      setStatus(`✓ ${r.ok} ホストを登録しました → ${s.hubUrl}`, 'ok');
    } else {
      setStatus(`一部失敗: ${r.ok} 成功 / ${r.failed} 失敗\n` + (r.errors || []).join('\n'), 'err');
    }
  } catch (e) {
    setStatus('一括登録失敗: ' + (e.message || e), 'err');
  } finally {
    btn.disabled = false;
  }
}

function openEditor(host) {
  const url = chrome.runtime.getURL('editor.html')
    + (host ? ('?host=' + encodeURIComponent(host)) : '');
  chrome.tabs.create({ url });
  window.close();
}

// ---- helpers ---------------------------------------------------------

function sendBg(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (r) => {
        if (chrome.runtime.lastError) resolve({ ok: false, reason: chrome.runtime.lastError.message });
        else resolve(r || { ok: false, reason: 'no response' });
      });
    } catch (e) {
      resolve({ ok: false, reason: String(e.message || e) });
    }
  });
}

// ---- init ------------------------------------------------------------

(async function init() {
  const tab = await PB.activeTab();
  activeUrl = (tab && PB.isHttpUrl(tab.url)) ? tab.url : '';
  activeHost = tab ? PB.hostFromUrl(tab.url) : '';
  $('activeHost').textContent = activeHost || '(なし)';

  await render();

  // settings menu toggle
  $('gear').addEventListener('click', () => $('settings').classList.toggle('show'));

  // settings persistence
  $('hub').addEventListener('change', saveHub);
  $('hub').addEventListener('blur', saveHub);
  bindToggle('devMode', PB.STORAGE.DEV, render);
  bindToggle('registeredOnly', PB.STORAGE.REGISTERED_ONLY);
  bindToggle('includeSession', PB.STORAGE.INCLUDE_SESSION);

  // actions
  $('syncNow').addEventListener('click', syncNow);
  $('pushAll').addEventListener('click', pushAll);
  $('openEditor').addEventListener('click', () => openEditor(activeHost));
  $('openEditorBlank').addEventListener('click', () => openEditor(''));
})();

// Paprika Bridge -- table-based cookie editor (Developer mode).
//
// Visualise + edit a host's cookies as a spreadsheet, then register the
// result to the Paprika hub (PUT /hosts/{host}). This is the MANUAL
// counterpart to the background auto-sync: no JSON text wrangling, just
// a table.
//
// Sources you can load into the table:
//   * "Chrome から読込": the host's live cookies from this browser.
//   * "Hub から読込":    whatever is already registered on the hub.
// Edit any cell, toggle the per-row ✓ to include/exclude it, add/remove
// rows, then "Paprika に登録" pushes the included rows.

const $ = (id) => document.getElementById(id);
const rowsEl = $('rows');

let HUB = '';

function setStatus(msg, kind = 'info') {
  const el = $('status');
  el.className = kind;
  el.textContent = msg;
}

function qsHost() {
  try { return PB.normaliseHost(new URLSearchParams(location.search).get('host') || ''); }
  catch (_) { return ''; }
}

// epoch seconds -> local "YYYY-MM-DD HH:MM" hint (or "セッション" when blank).
function expHint(epoch) {
  const n = Number(epoch);
  if (!epoch || !isFinite(n) || n <= 0) return 'セッション';
  const d = new Date(n * 1000);
  if (isNaN(d.getTime())) return '?';
  const pad = (x) => String(x).padStart(2, '0');
  const s = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
          + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return n * 1000 < Date.now() ? `${s} (期限切れ)` : s;
}

// ---- row construction ------------------------------------------------

function cell(cls, child) {
  const td = document.createElement('td');
  if (cls) td.className = cls;
  if (child) td.appendChild(child);
  return td;
}
function textInput(value, cls) {
  const i = document.createElement('input');
  i.type = 'text';
  i.value = value == null ? '' : String(value);
  if (cls) i.className = cls;
  i.addEventListener('input', refreshDerived);
  return i;
}
function checkbox(checked) {
  const i = document.createElement('input');
  i.type = 'checkbox';
  i.checked = !!checked;
  i.addEventListener('change', refreshDerived);
  return i;
}

// ``c`` is a paprika-shape cookie {name,value,domain,path,secure,httpOnly,
// sameSite?,expires?}.
function addRow(c = {}) {
  $('empty').style.display = 'none';
  const tr = document.createElement('tr');

  const inc = checkbox(true);
  inc.classList.add('inc');
  inc.addEventListener('change', () => tr.classList.toggle('off', !inc.checked));
  tr.appendChild(cell('center', inc));

  tr.appendChild(cell('name', textInput(c.name, 'f-name')));
  tr.appendChild(cell('value', textInput(c.value, 'f-value')));
  tr.appendChild(cell('domain', textInput(c.domain, 'f-domain')));
  tr.appendChild(cell('path', textInput(c.path || '/', 'f-path')));

  // expires: editable epoch + human hint
  const expWrap = document.createElement('td');
  expWrap.className = 'exp';
  const expIn = textInput(c.expires != null ? c.expires : '', 'f-exp');
  expIn.placeholder = 'セッション';
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = expHint(c.expires);
  expIn.addEventListener('input', () => { hint.textContent = expHint(expIn.value); });
  expWrap.appendChild(expIn);
  expWrap.appendChild(hint);
  tr.appendChild(expWrap);

  tr.appendChild(cell('center', (() => { const b = checkbox(c.secure); b.classList.add('f-secure'); return b; })()));
  tr.appendChild(cell('center', (() => { const b = checkbox(c.httpOnly); b.classList.add('f-httponly'); return b; })()));

  const ss = document.createElement('select');
  ss.className = 'ss f-samesite';
  for (const opt of ['', 'None', 'Lax', 'Strict']) {
    const o = document.createElement('option');
    o.value = opt; o.textContent = opt || '(未設定)';
    if ((c.sameSite || '') === opt) o.selected = true;
    ss.appendChild(o);
  }
  ss.addEventListener('change', refreshDerived);
  tr.appendChild(cell('', ss));

  const del = document.createElement('button');
  del.className = 'del';
  del.textContent = '🗑';
  del.title = 'この行を削除';
  del.addEventListener('click', () => { tr.remove(); refreshDerived(); });
  tr.appendChild(cell('center', del));

  rowsEl.appendChild(tr);
  refreshDerived();
}

// ---- read back -------------------------------------------------------

function rowToCookie(tr, includedOnly) {
  const inc = tr.querySelector('.inc').checked;
  if (includedOnly && !inc) return null;
  const v = (sel) => { const e = tr.querySelector(sel); return e ? e.value.trim() : ''; };
  const ck = (sel) => { const e = tr.querySelector(sel); return e ? e.checked : false; };
  const name = v('.f-name');
  const value = tr.querySelector('.f-value') ? tr.querySelector('.f-value').value : '';
  if (!name) return null; // a nameless row is incomplete -> skip
  const out = {
    name,
    value,
    domain: v('.f-domain'),
    path: v('.f-path') || '/',
    secure: ck('.f-secure'),
    httpOnly: ck('.f-httponly'),
  };
  const ss = v('.f-samesite');
  if (ss) out.sameSite = ss;
  const exp = v('.f-exp');
  if (exp) {
    const n = Number(exp);
    if (isFinite(n) && n > 0) out.expires = n;
  }
  return out;
}

function collect(includedOnly = true) {
  const out = [];
  for (const tr of rowsEl.querySelectorAll('tr')) {
    const c = rowToCookie(tr, includedOnly);
    if (c) out.push(c);
  }
  return out;
}

function refreshDerived() {
  const total = rowsEl.querySelectorAll('tr').length;
  const included = collect(true);
  $('rowmeta').innerHTML =
    `<span class="count">${included.length}</span> / ${total} 行を登録`;
  $('jsonpreview').textContent = JSON.stringify(included, null, 2);
  $('empty').style.display = total ? 'none' : '';
}

function clearRows() { rowsEl.replaceChildren(); refreshDerived(); }

// ---- actions ---------------------------------------------------------

async function loadFromChrome() {
  const host = PB.normaliseHost($('host').value);
  if (!host) { setStatus('ホストを入力してください', 'err'); return; }
  setStatus(`Chrome から ${host} の cookie を読み込み中 ...`, 'info');
  try {
    const cookies = await PB.cookiesForHost(host, { includeSession: true });
    clearRows();
    cookies.map(PB.toPaprikaCookie).forEach(addRow);
    setStatus(cookies.length
      ? `Chrome から ${cookies.length} 個読み込みました。編集して「Paprika に登録」。`
      : `Chrome に ${host} の cookie がありません（ログインしていない / 別プロファイル？）。`,
      cookies.length ? 'ok' : 'info');
  } catch (e) {
    setStatus('読み込み失敗: ' + e.message, 'err');
  }
}

async function loadFromHub() {
  const host = PB.normaliseHost($('host').value);
  if (!host) { setStatus('ホストを入力してください', 'err'); return; }
  if (!HUB) { setStatus('Hub URL を入力してください', 'err'); return; }
  setStatus(`Hub から ${host} の登録済み cookie を取得中 ...`, 'info');
  const rec = await PB.fetchHost(HUB, host);
  if (rec === undefined) { setStatus(`Hub に接続できません: ${HUB}`, 'err'); return; }
  if (rec === null) { setStatus(`Hub に ${host} は未登録です。`, 'info'); return; }
  clearRows();
  (rec.cookies || []).forEach(addRow);
  setStatus(`Hub から ${(rec.cookies || []).length} 個読み込みました。`, 'ok');
}

async function register() {
  const host = PB.normaliseHost($('host').value);
  if (!host) { setStatus('ホストを入力してください', 'err'); return; }
  if (!HUB) { setStatus('Hub URL を入力してください', 'err'); return; }
  const cookies = collect(true);
  if (cookies.length === 0
      && !confirm(`含める cookie が 0 個です。${host} の登録 cookie を空にしますか？`)) {
    return;
  }
  const btn = $('register');
  btn.disabled = true;
  setStatus(`${host} に ${cookies.length} 個の cookie を登録中 ...`, 'info');
  try {
    const existing = await PB.fetchHost(HUB, host);
    if (existing === undefined) { setStatus(`Hub に接続できません: ${HUB}`, 'err'); return; }
    await PB.putHost(HUB, host, cookies, existing);
    // mark fresh so the background auto-sync won't immediately re-push
    await PB.setStorage({ ['paprika.lastsync.' + host]: { at: Date.now(), n: cookies.length } });
    setStatus(`✓ ${host} に ${cookies.length} 個登録しました → ${HUB}/hosts/${host}`, 'ok');
  } catch (e) {
    setStatus('登録失敗: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

// ---- init ------------------------------------------------------------

(async function init() {
  const settings = await PB.getSettings();
  HUB = settings.hubUrl;
  $('hub').value = settings.hubUrlRaw || settings.hubUrl;
  $('hub').addEventListener('change', () => {
    HUB = PB.normaliseHubUrl($('hub').value);
    PB.setStorage({ [PB.STORAGE.HUB]: HUB });
  });

  const host = qsHost();
  $('host').value = host;

  $('loadChrome').addEventListener('click', loadFromChrome);
  $('loadHub').addEventListener('click', loadFromHub);
  $('addRow').addEventListener('click', () => addRow({ path: '/', domain: '.' + ($('host').value || '') }));
  $('register').addEventListener('click', register);

  // Visualise the current live state right away when a host is given.
  if (host) await loadFromChrome();
  else refreshDerived();
})();

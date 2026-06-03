
// ===== v2 Phase 7 Plugins tab — self-contained =====
(function () {
  let _plData = [];
  let _plInvocations = [];
  // Invocations pager state.
  const PL_INV_PAGE_SIZES = [10, 25, 50, 100];
  let _plInvPage = 0;
  function _plInvPageSize() {
    try {
      const stored = parseInt(localStorage.getItem('paprika.plInvPageSize') || '', 10);
      if (PL_INV_PAGE_SIZES.includes(stored)) return stored;
    } catch (_) {}
    return 25;
  }
  function _plInvPageSizeSet(n) {
    try { localStorage.setItem('paprika.plInvPageSize', String(n)); } catch (_) {}
  }

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function ago(iso) {
    if (!iso) return '—';
    // Borrow the global tt() helper from admin.js so units localise too.
    const T = window.tt || ((k, fb) => fb);
    try {
      const d = new Date(iso);
      const s = (Date.now() - d.getTime()) / 1000;
      if (s < 60)     return Math.round(s)       + T('plugins.ago.s', 's ago');
      if (s < 3600)   return Math.round(s/60)    + T('plugins.ago.m', 'm ago');
      if (s < 86400)  return Math.round(s/3600)  + T('plugins.ago.h', 'h ago');
      return Math.round(s/86400) + T('plugins.ago.d', 'd ago');
    } catch (e) { return '—'; }
  }

  function renderSummary() {
    const installedN = _plData.filter(p => p.installed).length;
    document.getElementById('plTotal').textContent     = installedN;
    document.getElementById('plAvailable').textContent = _plData.length;
    document.getElementById('plInvocations').textContent = _plInvocations.length;
    if (_plInvocations.length === 0) {
      document.getElementById('plOkRate').textContent = '—';
    } else {
      const ok = _plInvocations.filter(i => i.ok).length;
      document.getElementById('plOkRate').textContent =
        Math.round((ok / _plInvocations.length) * 100) + '%';
    }
    // Tab badge: show the catalog total so users see how many plugins
    // exist in paprika's universe (installed + advertised).
    const cnt = document.getElementById('cntPlugins');
    if (cnt) cnt.textContent = _plData.length;
  }

  function renderTable() {
    const T = window.tt || ((k, fb) => fb);
    const tbody = document.querySelector('#plTable tbody');
    if (!_plData.length) {
      tbody.innerHTML = '<tr><td colspan=8 class="empty">' + T('plugins.empty', 'catalog is empty — edit data/tools/catalog.json to advertise a plugin') + '</td></tr>';
      return;
    }
    const neverLabel        = T('plugins.never',          'never');
    const detailsLabel      = T('plugins.details',        'details');
    const statusInstalledTxt = T('plugins.status.installed', '✓ installed');
    const statusAvailableTxt = T('plugins.status.available', 'available');
    const localOnlyTxt       = T('plugins.status.localonly', 'local-only');

    const rows = _plData.map(p => {
      const lastInv = _plInvocations.find(i => i.plugin === p.name);
      const recent  = _plInvocations.filter(i => i.plugin === p.name);
      const okN     = recent.filter(i => i.ok).length;
      const recentSummary = recent.length === 0
        ? '<span style="color:#888;">—</span>'
        : `<span class="pl-status-ok">${okN}</span> / ${recent.length}`;
      const caps = (p.capabilities || []).map(c => `<span class="pl-chip">${escHtml(c)}</span>`).join('');

      // Status badge — installed vs catalog-only vs local-only.
      let statusBadge = '';
      if (p.installed && p.in_catalog) {
        statusBadge = `<span class="pl-status-badge pl-status-installed">${statusInstalledTxt}</span>`;
      } else if (p.installed && !p.in_catalog) {
        statusBadge = `<span class="pl-status-badge pl-status-localonly" title="installed but not in catalog.json">${localOnlyTxt}</span>`;
      } else {
        statusBadge = `<span class="pl-status-badge pl-status-available">${statusAvailableTxt}</span>`;
      }

      const versionStr = p.installed ? `${escHtml(p.installed_version || p.version || '')}` : `${escHtml(p.version || '')}`;
      const lastInvCell = p.installed
        ? (lastInv ? ago(lastInv.at) : `<span style="color:#888;">${neverLabel}</span>`)
        : '<span style="color:#bbb;">—</span>';
      const recentCell = p.installed
        ? recentSummary
        : '<span style="color:#bbb;">—</span>';

      // Action button: details (if installed) or install (if catalogued
      // but not yet installed). The install button calls
      // POST /admin/plugin_catalog/install/{name} which today returns
      // instructions for manual file placement; a future commit will
      // make it actually pull source.
      const actionCell = p.installed
        ? `<button class="pill pl-row-details" data-plugin="${escHtml(p.name)}"><iconify-icon icon="lucide:settings-2"></iconify-icon> ${detailsLabel}</button>`
        : `<button class="pill pl-row-install" data-plugin="${escHtml(p.name)}" style="--la-bg:#eef8ee; --la-bd:#7ab68a; --la-fg:#196b2c;"><iconify-icon icon="lucide:download"></iconify-icon> ${T('plugins.install', 'install')}</button>`;

      const rowCursor = p.installed ? 'cursor:pointer;' : '';
      return `<tr data-plugin="${escHtml(p.name)}" data-installed="${p.installed ? '1' : '0'}" style="${rowCursor}">
        <td>${statusBadge}</td>
        <td>
          <strong>${escHtml(p.name)}</strong>
          <span style="color:#888; font-size:0.85em; margin-left:6px;">v${versionStr}</span>
        </td>
        <td><span class="pl-chip">${escHtml(p.category || 'uncategorized')}</span></td>
        <td style="max-width:340px;">
          <div style="color:#444; font-size:0.92em;">${escHtml(p.summary || '')}</div>
          ${p.homepage ? `<a href="${escHtml(p.homepage)}" target="_blank" rel="noopener" style="font-size:0.78em; color:#1a5a8a;">${escHtml(p.homepage)}</a>` : ''}
        </td>
        <td>${caps || '<span style="color:#888;">—</span>'}</td>
        <td>${lastInvCell}</td>
        <td>${recentCell}</td>
        <td>${actionCell}</td>
      </tr>`;
    });
    tbody.innerHTML = rows.join('');
    tbody.querySelectorAll('tr').forEach(tr => {
      if (tr.getAttribute('data-installed') !== '1') return;
      tr.addEventListener('click', (e) => {
        // Ignore clicks on inner buttons (install button on uninstalled
        // rows handled below), but the row-click only fires for
        // installed rows so this is mostly a non-issue.
        const name = tr.getAttribute('data-plugin');
        if (name) openPluginModal(name);
      });
    });
    // Install buttons on catalog rows that aren't yet installed. Hits
    // POST /admin/plugin_catalog/install/{name} which today returns
    // manual-install instructions; future commits will let it actually
    // pull source.
    tbody.querySelectorAll('button.pl-row-install').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const name = btn.getAttribute('data-plugin');
        if (!name) return;
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<iconify-icon icon="lucide:loader-2"></iconify-icon> ...';
        try {
          const r = await fetch(
            '/admin/plugin_catalog/install/' + encodeURIComponent(name),
            { method: 'POST' },
          );
          const j = await r.json().catch(() => ({}));
          if (j.ok) {
            alert(`${name}: ${j.message}`);
            await loadPlugins();
          } else {
            // 200-with-ok=false (manual install) OR 4xx
            const msg = (j.message || 'install failed');
            const hint = (j.hint || '');
            alert(`${name}\n${msg}\n\n${hint}`);
          }
        } catch (e) {
          alert(`${name}: ${e}`);
        } finally {
          btn.disabled = false;
          btn.innerHTML = original;
        }
      });
    });
  }

  function renderInvocations() {
    const T = window.tt || ((k, fb) => fb);
    const tbody = document.querySelector('#plInvTable tbody');
    if (!_plInvocations.length) {
      tbody.innerHTML = '<tr><td colspan=7 class="empty">' + T('plugins.inv.empty', 'no invocations yet') + '</td></tr>';
      return;
    }
    const okLabel   = T('plugins.status.ok',   '✓ ok');
    const failLabel = T('plugins.status.fail', '✗ fail');
    const jobLabel  = T('plugins.job', 'job');
    // Slice the invocations array to the current page.
    const pageSize = _plInvPageSize();
    const total    = _plInvocations.length;
    const maxPage  = Math.max(0, Math.ceil(total / pageSize) - 1);
    if (_plInvPage > maxPage) _plInvPage = maxPage;
    if (_plInvPage < 0) _plInvPage = 0;
    const startIdx = _plInvPage * pageSize;
    const endIdx   = Math.min(total, startIdx + pageSize);
    const slice = _plInvocations.slice(startIdx, endIdx);
    const rows = slice.map(i => {
      const trig = i.trigger || i.source || '—';
      const trigCls = trig === 'preflight' ? 'preflight' : (trig === 'admin_ui' ? 'admin_ui' : '');
      const statusHtml = i.ok
        ? '<span class="pl-status-ok">' + okLabel + '</span>'
        : '<span class="pl-status-fail">' + failLabel + '</span>';
      const hostJob = [
        i.host  ? `<span class="pl-chip">${escHtml(i.host)}</span>`   : '',
        i.job_id ? `<span class="pl-chip">${jobLabel} ${escHtml(i.job_id.slice(0,8))}</span>` : '',
      ].filter(x => x).join(' ');
      return `<tr>
        <td style="font-size:0.85em; color:#666; white-space:nowrap;">${ago(i.at)}</td>
        <td><strong>${escHtml(i.plugin)}</strong></td>
        <td><span class="pl-chip action">${escHtml(i.action)}</span></td>
        <td>${statusHtml}${i.error ? ' <span style="color:#933; font-size:0.85em;" title="' + escHtml(i.error) + '">⚠</span>' : ''}</td>
        <td class="num">${i.elapsed_ms != null ? i.elapsed_ms + ' ms' : '—'}</td>
        <td>${hostJob || '<span style="color:#888;">—</span>'}</td>
        <td><span class="pl-trigger ${trigCls}">${escHtml(trig)}</span></td>
      </tr>`;
    });
    tbody.innerHTML = rows.join('');
    renderInvPager(total, startIdx, endIdx);
  }

  function renderInvPager(total, startIdx, endIdx) {
    const T = window.tt || ((k, fb) => fb);
    const host = document.getElementById('plInvPager');
    if (!host) return;
    if (total === 0) { host.innerHTML = ''; return; }
    const pageSize = _plInvPageSize();
    const maxPage  = Math.max(0, Math.ceil(total / pageSize) - 1);
    const prevDisabled = _plInvPage <= 0;
    const nextDisabled = _plInvPage >= maxPage;
    const opts = PL_INV_PAGE_SIZES
      .map(n => `<option value="${n}"${n === pageSize ? ' selected' : ''}>${n}</option>`)
      .join('');
    const prevLabel = T('plugins.pager.prev', 'prev');
    const nextLabel = T('plugins.pager.next', 'next');
    const pageLabel = T('plugins.pager.page', 'page');
    const perPage   = T('plugins.pager.perpage', 'per page');
    host.innerHTML = `
      <span style="color:#666;">${startIdx + 1}-${endIdx} / ${total}</span>
      <button class="pill" id="plInvPagerPrev" style="--la-bg:#f5f5fa; --la-bd:#bbc; --la-fg:#444;" ${prevDisabled ? 'disabled' : ''}>
        <iconify-icon icon="lucide:chevron-left"></iconify-icon> ${prevLabel}
      </button>
      <span style="color:#666;">${pageLabel} ${_plInvPage + 1} / ${maxPage + 1}</span>
      <button class="pill" id="plInvPagerNext" style="--la-bg:#f5f5fa; --la-bd:#bbc; --la-fg:#444;" ${nextDisabled ? 'disabled' : ''}>
        ${nextLabel} <iconify-icon icon="lucide:chevron-right"></iconify-icon>
      </button>
      <span style="margin-left:auto; color:#888; font-size:.85em;">
        ${perPage} <select id="plInvPagerSize" style="padding:2px 4px;">${opts}</select>
      </span>
    `;
    const prevBtn = document.getElementById('plInvPagerPrev');
    const nextBtn = document.getElementById('plInvPagerNext');
    const sizeSel = document.getElementById('plInvPagerSize');
    if (prevBtn) prevBtn.addEventListener('click', () => {
      if (_plInvPage > 0) { _plInvPage--; renderInvocations(); }
    });
    if (nextBtn) nextBtn.addEventListener('click', () => {
      _plInvPage++; renderInvocations();
    });
    if (sizeSel) sizeSel.addEventListener('change', () => {
      const n = parseInt(sizeSel.value, 10);
      if (PL_INV_PAGE_SIZES.includes(n)) {
        _plInvPageSizeSet(n);
        _plInvPage = 0;
        renderInvocations();
      }
    });
  }

  function openPluginModal(name) {
    const T = window.tt || ((k, fb) => fb);
    const p = _plData.find(x => x.name === name);
    if (!p) return;
    if (!p.installed) return;  // catalog-only entries have no action surface yet
    document.getElementById('plModalTitle').textContent = name + ' · ' + (p.installed_version || p.version || '');
    const body = document.getElementById('plModalBody');

    const myInvocations = _plInvocations.filter(i => i.plugin === name).slice(0, 20);
    const lastErrorEntry = myInvocations.find(i => !i.ok);

    const invokeLabel = T('plugins.modal.invoke', 'invoke');
    // Build per-action invocation form -- one tiny textarea + run button per action.
    const actionsHtml = (p.actions || []).map(act => `
      <div style="border:1px solid #e8ecf0; border-radius:6px; padding:10px 12px; margin:8px 0;">
        <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
          <strong>${escHtml(act)}</strong>
          <span style="flex:1;"></span>
          <button class="pill pl-modal-invoke" data-act="${escHtml(act)}" data-plugin="${escHtml(name)}">
            <iconify-icon icon="lucide:play"></iconify-icon> ${invokeLabel}
          </button>
        </div>
        <textarea data-params-for="${escHtml(act)}"
          style="width:100%; box-sizing:border-box; min-height:80px; font-family:ui-monospace,Consolas,monospace; font-size:12px; padding:6px 8px; border:1px solid #ccc; border-radius:4px;"
          placeholder='{"url": "https://example.com/"}'>{}</textarea>
        <div data-result-for="${escHtml(act)}"
          style="margin-top:6px; font-family:ui-monospace,Consolas,monospace; font-size:11px; color:#666; max-height:240px; overflow:auto;"></div>
      </div>
    `).join('');

    const capsHtml = (p.capabilities || []).map(c => `<span class="pl-chip">${escHtml(c)}</span>`).join('');
    body.innerHTML = `
      <div style="margin-bottom:14px;">
        <span class="pl-kind-badge kind-${escHtml(p.kind)}">${escHtml(p.kind)}</span>
        ${p.disabled ? '<span style="color:#933; font-weight:600; margin-left:8px;">' + T('plugins.disabled', 'DISABLED') + '</span>' : ''}
      </div>

      ${p.notes ? `<div style="background:#f6f8fa; border-left:3px solid #58a6ff; padding:8px 12px; margin:10px 0; border-radius:4px;">${escHtml(p.notes)}</div>` : ''}

      <h4 style="margin:14px 0 4px;">${T('plugins.modal.capabilities', 'Capabilities')}</h4>
      <div>${capsHtml || '<span style="color:#888;">—</span>'}</div>

      <h4 style="margin:18px 0 4px;">${T('plugins.modal.actions', 'Actions')}</h4>
      ${actionsHtml || '<span style="color:#888;">' + T('plugins.modal.noactions', 'no actions') + '</span>'}

      ${lastErrorEntry ? `
        <h4 style="margin:18px 0 4px; color:#933;">${T('plugins.modal.lastfail', 'Last failure')}</h4>
        <div style="background:#fee; border:1px solid #e8a4a0; padding:8px 12px; border-radius:4px; font-family:ui-monospace,Consolas,monospace; font-size:11px; white-space:pre-wrap; max-height:200px; overflow:auto;">${escHtml(lastErrorEntry.error || '')}</div>
      ` : ''}

      ${myInvocations.length ? `
        <h4 style="margin:18px 0 4px;">${T('plugins.modal.recent', 'Recent invocations')} (${myInvocations.length})</h4>
        <table style="width:100%; font-size:0.9em; border-collapse:collapse;">
          <thead><tr style="border-bottom:1px solid #eee; text-align:left; color:#666;">
            <th style="padding:4px 6px;">${T('plugins.modal.th.when', 'when')}</th><th style="padding:4px 6px;">${T('plugins.modal.th.action', 'action')}</th>
            <th style="padding:4px 6px;">${T('plugins.modal.th.status', 'status')}</th><th style="padding:4px 6px;">${T('plugins.modal.th.elapsed', 'elapsed')}</th>
            <th style="padding:4px 6px;">${T('plugins.modal.th.trigger', 'trigger')}</th>
          </tr></thead>
          <tbody>
            ${myInvocations.map(i => `
              <tr style="border-bottom:1px solid #f5f5f5;">
                <td style="padding:4px 6px; color:#666;">${ago(i.at)}</td>
                <td style="padding:4px 6px;">${escHtml(i.action)}</td>
                <td style="padding:4px 6px;">${i.ok ? '<span class="pl-status-ok">✓</span>' : '<span class="pl-status-fail">✗</span>'}</td>
                <td style="padding:4px 6px;" class="num">${i.elapsed_ms != null ? i.elapsed_ms + ' ms' : '—'}</td>
                <td style="padding:4px 6px;"><span class="pl-trigger">${escHtml(i.trigger || i.source || '—')}</span></td>
              </tr>`).join('')}
          </tbody>
        </table>
      ` : ''}
    `;
    document.getElementById('plModal').style.display = 'flex';
    // Re-apply i18n in case the modal body picks up new data-i18n attrs later.
    try { window.applyI18n && window.applyI18n(document.getElementById('plModalBody')); } catch (_) {}

    // Wire per-action invoke buttons inside the modal.
    body.querySelectorAll('button.pl-modal-invoke').forEach(btn => {
      btn.addEventListener('click', async () => {
        const act = btn.getAttribute('data-act');
        const plName = btn.getAttribute('data-plugin');
        const ta  = body.querySelector(`textarea[data-params-for="${act}"]`);
        const out = body.querySelector(`div[data-result-for="${act}"]`);
        const T2 = window.tt || ((k, fb) => fb);
        let params = {};
        try { params = JSON.parse(ta.value || '{}'); }
        catch (e) { out.innerHTML = '<span style="color:#933;">' + T2('plugins.modal.parseerror', 'JSON parse error') + ': ' + escHtml(e.message) + '</span>'; return; }
        out.innerHTML = '<span style="color:#888;">' + T2('plugins.modal.running', 'running…') + '</span>';
        btn.disabled = true;
        try {
          const r = await fetch('/admin/plugins/' + encodeURIComponent(plName) + '/invoke', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: act, params: params })
          });
          const txt = await r.text();
          let pretty;
          try { pretty = JSON.stringify(JSON.parse(txt), null, 2); }
          catch (e) { pretty = txt; }
          out.innerHTML = `<pre style="margin:0; white-space:pre-wrap; color:${r.ok ? '#196b2c' : '#933'};">${escHtml(pretty)}</pre>`;
        } catch (e) {
          out.innerHTML = '<span style="color:#933;">' + T2('plugins.modal.error', 'error') + ': ' + escHtml(String(e)) + '</span>';
        } finally {
          btn.disabled = false;
          // Refresh invocation list so the new call is visible.
          setTimeout(loadInvocations, 600);
        }
      });
    });
  }

  function closeModal() {
    document.getElementById('plModal').style.display = 'none';
  }

  async function loadPlugins() {
    const T = window.tt || ((k, fb) => fb);
    const tbody = document.querySelector('#plTable tbody');
    tbody.innerHTML = '<tr><td colspan=8 class="empty">' + T('plugins.loading', 'loading…') + '</td></tr>';
    try {
      // Catalog endpoint returns the union of catalog.json + currently-installed
      // plugins with `installed: bool` per entry.
      const r = await fetch('/admin/plugin_catalog');
      const j = await r.json();
      _plData = j.plugins || [];
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan=8 class="empty">' + T('plugins.modal.error', 'error') + ': ' + e + '</td></tr>';
      return;
    }
    await loadInvocations();
  }

  async function loadInvocations() {
    try {
      const r = await fetch('/admin/plugins/invocations?limit=1000');
      const j = await r.json();
      _plInvocations = j.invocations || [];
    } catch (e) {
      _plInvocations = [];
    }
    renderTable();
    renderInvocations();
    renderSummary();
  }

  async function deleteAllInvocations() {
    const T = window.tt || ((k, fb) => fb);
    const confirmMsg = T(
      'plugins.inv.deleteall.confirm',
      'Delete the entire invocations audit log? This cannot be undone.',
    );
    if (!confirm(confirmMsg)) return;
    const btn = document.getElementById('plInvDeleteAll');
    if (btn) btn.disabled = true;
    try {
      const r = await fetch('/admin/plugins/invocations', { method: 'DELETE' });
      if (!r.ok) {
        const t = await r.text();
        alert((T('plugins.modal.error', 'error')) + ': ' + t.slice(0, 200));
        return;
      }
      _plInvPage = 0;
      await loadInvocations();
    } catch (e) {
      alert((T('plugins.modal.error', 'error')) + ': ' + e);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function wire() {
    document.getElementById('plRefreshBtn').addEventListener('click', loadPlugins);
    document.getElementById('plInvDeleteAll').addEventListener('click', deleteAllInvocations);
    document.getElementById('plModalClose').addEventListener('click', closeModal);
    document.getElementById('plModal').addEventListener('click', (e) => {
      if (e.target.id === 'plModal') closeModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && document.getElementById('plModal').style.display === 'flex') {
        closeModal();
      }
    });
    // Refresh whenever the tab is opened
    document.querySelectorAll('[data-tab="plugins"]').forEach(btn => {
      btn.addEventListener('click', loadPlugins);
    });
    // Silent initial load so the badge count is accurate before the
    // operator opens the tab.
    loadPlugins();
    // Re-render dynamic table contents whenever i18next finishes init
    // (the very first render may happen before init resolves) or the
    // operator switches locale via the header dropdown.
    if (window.i18next) {
      window.i18next.on('languageChanged', () => { renderTable(); renderInvocations(); renderSummary(); });
      window.i18next.on('initialized',     () => { renderTable(); renderInvocations(); renderSummary(); });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();

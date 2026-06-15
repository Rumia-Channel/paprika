// admin-storage.js — Storage capacity (MinIO) trend panel.
//
// Fetches /storage/capacity (samples + current snapshot) and renders:
//   * a 6-card summary (total / used / free / paprika bucket / depletion-ETA / latest)
//   * a stacked-area + line Chart.js graph (used vs paprika-bucket over time)
//   * a coloured banner driven by the operator-set warn/crit thresholds
//   * an "今すぐサンプリング" button that POSTs /storage/capacity/sample.
//
// Auto-refresh: only while the #storage tab is visible AND the browser tab
// is foregrounded -- mirrors the AI activity tab so it adds no load when
// you're elsewhere.

(function () {
  'use strict';

  const PANEL = () => document.querySelector('.panel[data-panel="storage"]');
  const TAB_BTN = () => document.querySelector('.tab[data-tab="storage"]');

  let _chart = null;
  let _refreshTimer = null;
  let _firstRenderDone = false;

  function _fmtBytes(n) {
    if (!Number.isFinite(n) || n <= 0) return '—';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 100 || i === 0 ? 0 : (v >= 10 ? 1 : 2)) + ' ' + units[i];
  }

  function _fmtPct(p) {
    if (!Number.isFinite(p)) return '—';
    return p.toFixed(2) + '%';
  }

  function _fmtCountShort(n) {
    if (!Number.isFinite(n) || n <= 0) return '0';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }

  function _fmtTsShort(iso) {
    if (!iso) return '—';
    try {
      // backend ts is UTC; show as local (JST) for the operator.
      const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      return `${y}-${m}-${day} ${hh}:${mm}`;
    } catch (_) {
      return iso;
    }
  }

  function _fmtDuration(s) {
    if (!Number.isFinite(s) || s <= 0) return '—';
    const days = Math.floor(s / 86400);
    const hours = Math.floor((s % 86400) / 3600);
    if (days >= 2) return `${days}日`;
    if (days >= 1) return `${days}日 ${hours}時間`;
    if (hours >= 1) return `${hours}時間`;
    return `${Math.max(1, Math.floor(s / 60))}分`;
  }

  // ---- ETA: linear regression of free_bytes over the last 24h ---------
  // Avoids reacting to single-burst writes. Reports "枯渇まで N 日" + the
  // slope (GiB/day). Skips when there's <2 samples in the window, or
  // when the slope is flat / improving (free isn't shrinking).
  function _depletionEta(samples) {
    if (!samples || samples.length < 2) return { eta_s: null, slope_bps: 0 };
    const now = Date.now();
    const cutoff = now - 24 * 3600 * 1000;
    const recent = samples.filter(s => {
      try { return new Date(s.ts.endsWith('Z') ? s.ts : s.ts + 'Z').getTime() >= cutoff; }
      catch (_) { return false; }
    });
    if (recent.length < 2) return { eta_s: null, slope_bps: 0 };
    // linear regression: free_bytes vs ts_seconds
    let n = 0, sx = 0, sy = 0, sxx = 0, sxy = 0;
    const t0 = new Date(recent[0].ts.endsWith('Z') ? recent[0].ts : recent[0].ts + 'Z').getTime() / 1000;
    for (const s of recent) {
      const x = new Date(s.ts.endsWith('Z') ? s.ts : s.ts + 'Z').getTime() / 1000 - t0;
      const y = s.free_bytes;
      n++; sx += x; sy += y; sxx += x * x; sxy += x * y;
    }
    const denom = n * sxx - sx * sx;
    if (denom <= 0) return { eta_s: null, slope_bps: 0 };
    const slope = (n * sxy - sx * sy) / denom;  // bytes/sec; negative when shrinking
    const intercept = (sy - slope * sx) / n;
    const free_now = slope * (recent[recent.length - 1] && (new Date(recent[recent.length - 1].ts.endsWith('Z') ? recent[recent.length - 1].ts : recent[recent.length - 1].ts + 'Z').getTime() / 1000 - t0)) + intercept;
    if (slope >= 0) return { eta_s: null, slope_bps: slope };  // not shrinking
    // time when free hits 0 from now
    const eta_s = -free_now / slope;
    return { eta_s, slope_bps: slope };
  }

  function _ensureChart(canvas) {
    if (!canvas) return null;
    if (_chart) return _chart;
    if (typeof Chart === 'undefined') return null;
    _chart = new Chart(canvas, {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: {
            ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
            grid: { color: 'rgba(0,0,0,0.04)' },
          },
          y: {
            beginAtZero: true,
            ticks: { callback: (v) => _fmtBytes(v) },
            grid: { color: 'rgba(0,0,0,0.06)' },
          },
        },
        plugins: {
          legend: { position: 'bottom', labels: { font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${_fmtBytes(ctx.parsed.y)}`,
            },
          },
        },
      },
    });
    return _chart;
  }

  function _renderChart(samples) {
    const canvas = document.getElementById('storageChart');
    const empty = document.getElementById('storageChartEmpty');
    if (!canvas) return;
    if (!samples || samples.length === 0) {
      canvas.style.display = 'none';
      if (empty) empty.style.display = 'flex';
      if (_chart) { try { _chart.destroy(); } catch (_) {} _chart = null; }
      return;
    }
    canvas.style.display = '';
    if (empty) empty.style.display = 'none';

    const chart = _ensureChart(canvas);
    if (!chart) return;

    // Downsample if more than ~400 points so the chart stays snappy.
    let pts = samples;
    if (pts.length > 400) {
      const step = Math.ceil(pts.length / 400);
      pts = pts.filter((_, i) => i % step === 0);
    }

    const labels = pts.map(s => _fmtTsShort(s.ts));
    const total = pts.map(s => s.total_bytes);
    const used = pts.map(s => s.used_bytes);
    const free = pts.map(s => s.free_bytes);
    const bucket = pts.map(s => s.bucket_usage_bytes);

    chart.data.labels = labels;
    chart.data.datasets = [
      {
        label: '総容量',
        data: total,
        borderColor: 'rgba(120,120,120,0.6)',
        backgroundColor: 'rgba(120,120,120,0.05)',
        borderWidth: 1,
        borderDash: [4, 4],
        pointRadius: 0,
        fill: false,
        tension: 0.1,
      },
      {
        label: '使用量 (MinIO 全体)',
        data: used,
        borderColor: 'rgba(212, 80, 80, 1)',
        backgroundColor: 'rgba(212, 80, 80, 0.18)',
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        tension: 0.2,
      },
      {
        label: 'paprika バケット',
        data: bucket,
        borderColor: 'rgba(58,92,168,1)',
        backgroundColor: 'rgba(58,92,168,0.18)',
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        tension: 0.2,
      },
      {
        label: '空き',
        data: free,
        borderColor: 'rgba(35,140,80,1)',
        backgroundColor: 'rgba(35,140,80,0.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: false,
        tension: 0.2,
        borderDash: [2, 3],
      },
    ];
    chart.update();
  }

  function _renderSummary(data) {
    const cur = data && data.current;
    const t = data && data.thresholds;
    if (t) {
      const w = document.getElementById('stWarnPct');
      const c = document.getElementById('stCritPct');
      if (w) w.textContent = String(t.warn_percent);
      if (c) c.textContent = String(t.crit_percent);
    }

    if (!cur) {
      ['stKvTotal','stKvUsed','stKvFree','stKvBucket','stKvEta','stKvLatest'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '—';
      });
      ['stKvUsedPct','stKvObjects','stKvEtaDetail','stKvLatestHub'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = cur === null ? 'まだサンプルがありません' : '—';
      });
      _renderBanner(null);
      _renderTabBadge(null);
      return;
    }

    const setText = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setText('stKvTotal', _fmtBytes(cur.total_bytes));
    setText('stKvUsed', _fmtBytes(cur.used_bytes));
    setText('stKvUsedPct', cur.used_percent != null ? _fmtPct(cur.used_percent) + ' 使用' : '—');
    setText('stKvFree', _fmtBytes(cur.free_bytes));
    setText('stKvBucket', _fmtBytes(cur.bucket_usage_bytes));
    setText('stKvObjects', _fmtCountShort(cur.bucket_object_count) + ' objects');
    setText('stKvLatest', _fmtTsShort(cur.ts));
    setText('stKvLatestHub', cur.hub_id ? `from ${cur.hub_id}${cur.healthy ? '' : ' · ' + (cur.note || 'unhealthy')}` : '');

    // depletion ETA
    const eta = _depletionEta(data.samples || []);
    const etaEl = document.getElementById('stKvEta');
    const etaDetail = document.getElementById('stKvEtaDetail');
    if (etaEl) {
      if (eta.eta_s == null) {
        etaEl.textContent = '減少なし';
        etaEl.style.color = '#196b2c';
        if (etaDetail) etaDetail.textContent = '24h 内に空きが減っていません';
      } else {
        etaEl.textContent = _fmtDuration(eta.eta_s);
        // pessimistic: <2 days = red, <7 days = amber, else green
        if (eta.eta_s < 2 * 86400) etaEl.style.color = '#c0392b';
        else if (eta.eta_s < 7 * 86400) etaEl.style.color = '#b8830f';
        else etaEl.style.color = '#196b2c';
        const gibPerDay = (-eta.slope_bps * 86400) / (1024 ** 3);
        if (etaDetail) etaDetail.textContent = `減少率: ${gibPerDay.toFixed(2)} GiB/日 (24h 平均)`;
      }
    }

    _renderBanner(cur);
    _renderTabBadge(cur);
  }

  function _renderBanner(cur) {
    const el = document.getElementById('stBanner');
    if (!el) return;
    if (!cur) { el.style.display = 'none'; return; }
    if (cur.status === 'critical') {
      el.style.display = '';
      el.style.background = '#fbeaea';
      el.style.color = '#9c1f1f';
      el.style.border = '1px solid #e0a0a0';
      el.innerHTML = `<iconify-icon icon="lucide:alert-octagon"></iconify-icon> <strong>危険</strong>: MinIO 使用率 ${_fmtPct(cur.used_percent)} (空き ${_fmtBytes(cur.free_bytes)})。即時の対応が必要です。`;
    } else if (cur.status === 'warn') {
      el.style.display = '';
      el.style.background = '#fdf5e8';
      el.style.color = '#8b6112';
      el.style.border = '1px solid #d8b977';
      el.innerHTML = `<iconify-icon icon="lucide:alert-triangle"></iconify-icon> <strong>警告</strong>: MinIO 使用率 ${_fmtPct(cur.used_percent)} (空き ${_fmtBytes(cur.free_bytes)})。容量増設の検討を。`;
    } else if (!cur.healthy) {
      el.style.display = '';
      el.style.background = '#f2f2f7';
      el.style.color = '#555';
      el.style.border = '1px solid #ccd';
      el.innerHTML = `<iconify-icon icon="lucide:info"></iconify-icon> MinIO が応答していません: ${cur.note || 'unknown'}`;
    } else {
      el.style.display = 'none';
    }
  }

  // タブの右側に小さなバッジ (OK = 緑 / warn = 黄 / crit = 赤)
  function _renderTabBadge(cur) {
    const badge = document.getElementById('cntStorageStatus');
    if (!badge) return;
    if (!cur) {
      badge.textContent = '—';
      badge.style.background = '#e8e8e8';
      badge.style.color = '#555';
      return;
    }
    if (cur.status === 'critical') {
      badge.textContent = Math.round(cur.used_percent) + '%';
      badge.style.background = '#c0392b';
      badge.style.color = '#fff';
    } else if (cur.status === 'warn') {
      badge.textContent = Math.round(cur.used_percent) + '%';
      badge.style.background = '#d4a13d';
      badge.style.color = '#fff';
    } else if (!cur.healthy) {
      badge.textContent = '!';
      badge.style.background = '#888';
      badge.style.color = '#fff';
    } else {
      badge.textContent = Math.round(cur.used_percent) + '%';
      badge.style.background = '#e0efe0';
      badge.style.color = '#196b2c';
    }
  }

  function _selectedDays() {
    const sel = document.getElementById('stRangeDays');
    if (!sel) return 7;
    const n = parseInt(sel.value, 10);
    return Number.isFinite(n) && n > 0 ? n : 7;
  }

  async function _fetchAndRender() {
    try {
      const r = await fetch(`/storage/capacity?days=${_selectedDays()}&_=${Date.now()}`, {
        headers: { 'Accept': 'application/json' },
      });
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        console.warn('storage/capacity', r.status, txt);
        if (r.status === 503) _showFatal('MariaDB が利用できないため、サンプルが保存されていません。');
        return;
      }
      const data = await r.json();
      _renderSummary(data);
      _renderChart(data.samples || []);
      _firstRenderDone = true;
    } catch (e) {
      console.error('storage fetch failed:', e);
    }
  }

  function _showFatal(msg) {
    const el = document.getElementById('stBanner');
    if (!el) return;
    el.style.display = '';
    el.style.background = '#fbeaea';
    el.style.color = '#9c1f1f';
    el.style.border = '1px solid #e0a0a0';
    el.innerHTML = `<iconify-icon icon="lucide:alert-octagon"></iconify-icon> ${msg}`;
  }

  async function _sampleNow() {
    const btn = document.getElementById('stSampleNowBtn');
    if (btn) { btn.disabled = true; btn.style.opacity = '.6'; }
    try {
      const r = await fetch('/storage/capacity/sample', { method: 'POST' });
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        alert(`サンプル取得失敗: HTTP ${r.status}\n${txt}`);
      }
    } catch (e) {
      alert('サンプル取得失敗: ' + e);
    } finally {
      if (btn) { btn.disabled = false; btn.style.opacity = ''; }
      await _fetchAndRender();
    }
  }

  function _isPanelActive() {
    const p = PANEL();
    return !!(p && p.classList.contains('active'));
  }

  function _startRefreshTimer() {
    _stopRefreshTimer();
    // Refresh every 30s while the panel is visible.
    _refreshTimer = setInterval(() => {
      if (_isPanelActive() && document.visibilityState === 'visible') {
        _fetchAndRender();
      }
    }, 30000);
  }

  function _stopRefreshTimer() {
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  }

  function _onActivated() {
    _fetchAndRender();
    _startRefreshTimer();
  }

  function _wire() {
    const tab = TAB_BTN();
    if (tab) {
      tab.addEventListener('click', () => {
        // setTab() in admin-core runs synchronously on the same click; the
        // panel is .active by the time we run.
        setTimeout(_onActivated, 0);
      });
    }
    const refreshBtn = document.getElementById('stRefreshBtn');
    if (refreshBtn) refreshBtn.addEventListener('click', _fetchAndRender);
    const sampleBtn = document.getElementById('stSampleNowBtn');
    if (sampleBtn) sampleBtn.addEventListener('click', _sampleNow);
    const rangeSel = document.getElementById('stRangeDays');
    if (rangeSel) rangeSel.addEventListener('change', _fetchAndRender);

    // Initial: if URL hash deep-links to #storage, render immediately.
    if ((location.hash || '').replace(/^#/, '').trim() === 'storage') {
      _onActivated();
    }
    // Otherwise still do a single background fetch so the tab badge is
    // populated on first paint — but don't start the timer until the
    // operator opens the tab.
    else if (!_firstRenderDone) {
      _fetchAndRender();
    }
    // Listen for hash changes (back/forward / hash-driven nav)
    window.addEventListener('hashchange', () => {
      if ((location.hash || '').replace(/^#/, '').trim() === 'storage') _onActivated();
      else _stopRefreshTimer();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _wire);
  } else {
    _wire();
  }
})();

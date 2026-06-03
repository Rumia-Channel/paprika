// Simple-engine macro builder
// =========================================================================
//
// Stack of rows that compile to a paprika-client Python script. Each
// row is one browser action; submit converts the list to source code
// and runs it as mode=rerun. The macro list is persisted in
// localStorage so a page reload doesn't lose the work-in-progress.
//
// Emit a Python string literal for ``s``. When ``s`` contains a
// {curly-brace} interpolation (e.g. ``{i}`` or ``{i+1}``), emit an
// f-string so the loop iteration variable resolves at runtime;
// otherwise emit a plain string. Backslashes and double-quotes are
// escaped either way; curly braces inside f-strings are kept as-is
// because that's where the substitution happens.
function _simpleEmitLit(s) {
  const str = String(s == null ? '' : s);
  const hasInterp = /\{[^{}]+\}/.test(str);
  const escaped = str.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  return hasInterp ? `f"${escaped}"` : `"${escaped}"`;
}

// SIMPLE_ACTIONS is the catalog: each entry defines the dropdown
// option (icon + label), the placeholder hint for the param input,
// and a compile() function returning ONE Python statement WITHOUT a
// leading indent. compileSimpleMacroToCode() prepends the
// depth-appropriate indent (12 spaces base + 4 per nesting level).
//
// Detail strings flow through _simpleEmitLit() so writing ``{i+1}``
// inside e.g. a Click (visual) description gets compiled to an
// f-string and resolves to the current Loop iteration index.
const SIMPLE_ACTIONS = [
  {
    value: 'navigate', category: '移動', icon: 'lucide:navigation', label: 'Navigate',
    placeholder: 'https://example.com/',
    compile: (d) => `await page.goto(${_simpleEmitLit(d.trim() || 'about:blank')})`,
  },
  {
    value: 'back', category: '移動', icon: 'lucide:arrow-left', label: 'Back',
    placeholder: '(なし)',
    compile: () => `await page.back()`,
  },
  {
    value: 'forward', category: '移動', icon: 'lucide:arrow-right', label: 'Forward',
    placeholder: '(なし)',
    compile: () => `await page.forward()`,
  },
  {
    value: 'history_first', category: '移動', icon: 'lucide:rewind', label: '履歴の最初へ',
    placeholder: '(なし)',
    compile: () => `await page.history_first()`,
  },
  {
    value: 'click', category: '操作', icon: 'lucide:mouse-pointer-click', label: 'Click (CSS)',
    placeholder: 'CSS selector (例: .btn-primary)',
    compile: (d) => `await page.click(${_simpleEmitLit(d.trim())})`,
  },
  {
    value: 'type', category: '操作', icon: 'lucide:type', label: 'Type',
    placeholder: 'focus 中の要素に挿入する文字列',
    compile: (d) => `await page.type(${_simpleEmitLit(d)})`,
  },
  {
    value: 'fill', category: '操作', icon: 'lucide:edit-3', label: 'Fill',
    placeholder: 'selector ⇒ value  (例: #search ⇒ pizza)',
    compile: (d) => {
      const parts = String(d).split(/⇒|=>|\|/);
      const sel = (parts[0] || '').trim();
      const val = parts.slice(1).join('|').trim();
      return `await page.fill(${_simpleEmitLit(sel)}, ${_simpleEmitLit(val)})`;
    },
  },
  {
    value: 'press', category: '操作', icon: 'lucide:keyboard', label: 'Press key',
    placeholder: 'Enter / Backspace x3 / Ctrl+A',
    compile: (d) => {
      const s = String(d).trim();
      const m = s.match(/^(.+?)\s*[xX]\s*(\d+)\s*$/);
      if (m) {
        return `await page.press(${_simpleEmitLit(m[1].trim())}, count=${parseInt(m[2],10)})`;
      }
      return `await page.press(${_simpleEmitLit(s)})`;
    },
  },
  {
    value: 'scroll', category: '操作', icon: 'lucide:scroll', label: 'Scroll',
    placeholder: 'down 800 / up 400 / left 200 / right 200',
    compile: (d) => {
      const parts = String(d).trim().split(/\s+/);
      const dir = (parts[0] || 'down').toLowerCase();
      const px  = parseInt(parts[1], 10) || 800;
      return `await page.scroll(${JSON.stringify(dir)}, ${px})`;
    },
  },
  {
    value: 'wait', category: '待ち', icon: 'lucide:clock', label: 'Wait',
    placeholder: 'seconds (例: 3)',
    compile: (d) => {
      const sec = parseFloat(d) || 1;
      return `await page.wait_for(seconds=${sec})`;
    },
  },
  {
    value: 'vision', category: 'AI', icon: 'lucide:eye', label: 'Agent (Visual)',
    placeholder: '日本語/英語の説明 (例: the {i+1}th thumbnail / 再生ボタン)',
    compile: (d) => `await page.agent(${_simpleEmitLit(d.trim())}, engine="cogagent", max_steps=2)`,
  },
  {
    value: 'agent_dom', category: 'AI', icon: 'lucide:list-tree', label: 'Agent (DOM)',
    placeholder: '日本語/英語の説明 (DOM/アクセシビリティツリーから判断)',
    compile: (d) => `await page.agent(${_simpleEmitLit(d.trim())}, engine="qwen", max_steps=2)`,
  },
  {
    value: 'agent', category: 'AI', icon: 'lucide:sparkles', label: 'Agent (multi-step)',
    placeholder: '多段操作の説明 (例: log in with my credentials)',
    compile: (d) => `await page.agent(${_simpleEmitLit(d.trim())}, max_steps=5)`,
  },
  {
    value: 'capture', category: '取り込み', icon: 'lucide:camera', label: 'Capture',
    placeholder: 'label (任意; 例: step-{i+1})',
    compile: (d) => `await page.capture(${_simpleEmitLit((d || 'capture').trim())})`,
  },
  {
    value: 'dlvideo', category: '取り込み', icon: 'lucide:download', label: 'Download video',
    placeholder: 'URL (任意; 省略時は現在ページ)',
    compile: (d) => {
      const u = String(d).trim();
      return u
        ? `await page.download_video(url=${_simpleEmitLit(u)})`
        : `await page.download_video()`;
    },
  },
  // -- tab management --------------------------------------------------
  // ``page`` in the generated script is a Session (= Page + tab
  // container). page.open(url) appends a tab; page[i] / page[-1] index
  // by position; page[i].close() smart-closes (last tab in the session
  // -> DELETE session, otherwise DELETE that tab only). No new API
  // needed -- these macros just stamp out the right one-liners.
  {
    value: 'open_tab', category: 'タブ', icon: 'lucide:square-plus', label: 'Open new tab',
    placeholder: 'https://example.com/  (空欄なら about:blank)',
    compile: (d) => {
      const u = String(d || '').trim();
      return u
        ? `await page.open(${_simpleEmitLit(u)})`
        : `await page.open()`;
    },
  },
  {
    value: 'switch_tab', category: 'タブ', icon: 'lucide:arrow-left-right', label: 'Switch to tab #N',
    placeholder: 'タブ番号 (0 始まり; -1 で最後のタブ)',
    compile: (d) => {
      const raw = String(d || '').trim();
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n)) {
        return `raise ValueError("switch_tab: invalid tab index " + ${_simpleEmitLit(raw)})`;
      }
      return `await page.switch(${n})`;
    },
  },
  {
    value: 'close_tab', category: 'タブ', icon: 'lucide:x-circle', label: 'Close tab #N',
    placeholder: 'タブ番号 (0 始まり; 例: 1)',
    compile: (d) => {
      const raw = String(d || '').trim();
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n) || n < 0) {
        return `raise ValueError("close_tab: invalid tab index " + ${_simpleEmitLit(raw)})`;
      }
      return `await page[${n}].close()`;
    },
  },
  {
    value: 'close_last_tab', category: 'タブ', icon: 'lucide:x', label: 'Close last tab',
    placeholder: '(なし)',
    compile: () => `await page[-1].close()`,
  },
  // -- control flow ----------------------------------------------------
  // Loop begin: opens a `for {var} in range(N):` block. Subsequent
  // rows are auto-indented (depth+1) until the matching `End loop`.
  // The iteration variable name (`i`, `j`, `k`, ...) is picked by
  // depth, so inner rows can reference it via {i} / {i+1} in their
  // detail string.
  {
    value: 'loop', category: '制御', icon: 'lucide:repeat', label: 'Loop (begin)',
    placeholder: '反復回数 (例: 5)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'loop_end', category: '制御', icon: 'lucide:corner-down-left', label: 'End loop',
    placeholder: '(なし)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  // -- conditional (if / else / end if) --------------------------------
  // 3 種類の条件タイプ:
  //   If (CSS)    -- CSS セレクタの存在チェック (確定的、LLM 不要)
  //   If (Agent)  -- 自然言語の yes/no 質問を LLM (Qwen) に投げる
  //   If (Visual) -- (将来) スクリーンショット + CogAgent で yes/no
  // detail 欄: CSS 版はセレクタ、Agent 版は質問文。
  // 後続の行は `if (...):` ブロック内にインデントされ、End if で閉じる。
  // 任意で間に `Else` を挟むと else 分岐になる。
  {
    value: 'if_css', category: '制御', icon: 'lucide:braces', label: 'If (CSS)',
    placeholder: 'CSS selector (例: .login-btn)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'if_agent', category: '制御', icon: 'lucide:help-circle', label: 'If (Agent)',
    placeholder: 'yes/no 質問 (例: ログイン画面が表示されているか?)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'if_else', category: '制御', icon: 'lucide:git-branch', label: 'Else',
    placeholder: '(なし)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'if_end', category: '制御', icon: 'lucide:corner-down-left', label: 'End if',
    placeholder: '(なし)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
];

// Iteration variable name for a given loop nesting depth.
// 7+ deep is genuinely unusual; fall back to i7 / i8 / ... and let
// the user rename if they really need it.
const _SIMPLE_ITER_VARS = ['i', 'j', 'k', 'l', 'm', 'n'];
function _simpleIterVar(depth) {
  return _SIMPLE_ITER_VARS[depth] || ('i' + depth);
}

// Walk the row list and compute (depth_at_each_row, final_depth,
// warnings). depth starts at 0 and is incremented by a `loop` row
// (after the row itself is processed) and decremented by a
// `loop_end` (BEFORE the row is processed, so the end-marker
// renders at the outer depth). When `loop_end` appears without a
// matching `loop`, we clamp to 0 and emit a warning.
function _simpleComputeDepths() {
  // Tracks both `Loop ... End loop` and `If ... [Else] ... End if`
  // nesting. The two share a single ``depth`` counter:
  //   * Openers (loop / if_css / if_agent): emit at current depth, then ++
  //   * Closers (loop_end / if_end): -- then emit at the new (smaller) depth
  //   * Else (if_else): emits at depth-1 (the level of the matching `if:`),
  //     but does NOT change ``depth`` -- the rows that follow are still
  //     inside the else-branch body
  let depth = 0;
  const depths = [];
  const warns = [];
  _simpleRows.forEach((row, idx) => {
    if (row.action === 'loop_end' || row.action === 'if_end') {
      const label = row.action === 'loop_end' ? 'End loop' : 'End if';
      if (depth === 0) {
        warns.push(`row ${idx + 1}: extra "${label}" with no matching opener`);
        depths.push(0);
      } else {
        depth -= 1;
        depths.push(depth);
      }
    } else if (row.action === 'if_else') {
      // `else:` line sits one indent level outside the body (i.e. at
      // the matching `if:` depth).
      if (depth === 0) {
        warns.push(`row ${idx + 1}: "Else" outside any If block`);
        depths.push(0);
      } else {
        depths.push(depth - 1);
      }
    } else {
      depths.push(depth);
      if (row.action === 'loop' || row.action === 'if_css' || row.action === 'if_agent') {
        depth += 1;
      }
    }
  });
  if (depth > 0) {
    warns.push(`${depth} unmatched block opener(s)`);
  }
  return { depths, finalDepth: depth, warns };
}

const SIMPLE_ROWS_KEY = 'paprika.submit.simpleRows';

// In-memory macro state: array of {action: "vision"|..., detail: "..."}
let _simpleRows = (function loadSimpleRows() {
  try {
    const raw = localStorage.getItem(SIMPLE_ROWS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(r => ({
      action: typeof r.action === 'string' ? r.action : 'navigate',
      detail: typeof r.detail === 'string' ? r.detail : '',
    }));
  } catch (_) { return []; }
})();

function saveSimpleRows() {
  try { localStorage.setItem(SIMPLE_ROWS_KEY, JSON.stringify(_simpleRows)); }
  catch (_) {}
}

function _simpleActionByValue(v) {
  return SIMPLE_ACTIONS.find(a => a.value === v) || SIMPLE_ACTIONS[0];
}

// Build the action picker HTML for one row. Native <select> can't
// render icons inside <option>, so we use a button + popover combo:
//   .ap-button         the always-visible chip ([icon] label ▾)
//   .ap-popover        the dropdown panel; one .ap-group-header per
//                      category, then .ap-item buttons under it
// The current selection is highlighted via data-selected. Click on
// an .ap-item sets that row's action and triggers re-render.
function _simpleActionPicker(selectedValue) {
  const spec = _simpleActionByValue(selectedValue);
  const groups = new Map();
  for (const a of SIMPLE_ACTIONS) {
    const cat = a.category || 'その他';
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(a);
  }
  let body = '';
  for (const [cat, items] of groups.entries()) {
    body += `<div class="ap-group-header">${cat}</div>`;
    body += items.map(a =>
      `<button type="button" class="ap-item" data-value="${a.value}"`
      + `${a.value === selectedValue ? ' data-selected' : ''}>`
      + `<iconify-icon icon="${a.icon}"></iconify-icon>`
      + `<span>${a.label}</span>`
      + `</button>`
    ).join('');
  }
  return `
    <div class="simple-action-picker" data-value="${selectedValue}">
      <button type="button" class="ap-button">
        <iconify-icon icon="${spec.icon}" style="font-size:1.15em; min-width:1.4em; color:#555;"></iconify-icon>
        <span class="ap-current-label">${spec.label}</span>
        <span class="ap-caret">▾</span>
      </button>
      <div class="ap-popover" hidden>${body}</div>
    </div>
  `;
}

// One-shot global handlers (installed via the IIFE guard so a re-eval
// of this script doesn't stack listeners). Close any open popover on
// outside-click and on Escape.
(function _installSimpleActionPickerHandlers() {
  if (window.__simpleActionPickerHandlersInstalled) return;
  window.__simpleActionPickerHandlersInstalled = true;
  document.addEventListener('click', (ev) => {
    // If the click is inside an .ap-popover or its toggling button,
    // let the per-row handler deal with it. Otherwise close all.
    const inside = ev.target.closest('.simple-action-picker');
    document.querySelectorAll('.ap-popover').forEach(p => {
      if (!inside || !inside.contains(p)) p.hidden = true;
    });
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      document.querySelectorAll('.ap-popover').forEach(p => p.hidden = true);
    }
  });
})();

function renderSimpleRows() {
  const host = document.getElementById('simpleRows');
  if (!host) return;
  // Auto-add a starter row when the user first lands on simple mode
  // and has no saved macro yet -- friendlier than an empty box.
  if (_simpleRows.length === 0) {
    _simpleRows.push({ action: 'navigate', detail: '' });
  }
  // Compute per-row depth from loop/loop_end markers. Used to indent
  // rows visually (left-padding) and to warn about mismatched pairs.
  const { depths, warns } = _simpleComputeDepths();
  // Rebuild from scratch. Macros are small (typically < 20 rows) so
  // full re-render is cheaper than diff bookkeeping.
  host.innerHTML = '';
  if (warns.length) {
    const warnBar = document.createElement('div');
    warnBar.style.cssText = 'padding:6px 10px; background:#fff5e0; border:1px solid #e0b870; border-radius:4px; font-size:.85em; color:#8a5a00;';
    warnBar.textContent = '⚠ ' + warns.join('; ');
    host.appendChild(warnBar);
  }
  _simpleRows.forEach((row, idx) => {
    const d = depths[idx];
    const spec = _simpleActionByValue(row.action);
    const isLoopEnd = row.action === 'loop_end';
    const isLoopBegin = row.action === 'loop';
    const wrap = document.createElement('div');
    wrap.className = 'simple-row';
    // Visual indent per nesting depth so loop bodies stand out.
    // Loop-begin rows live at the OUTER depth (their body is the
    // indented part), so depth[idx] for the begin row is the outer
    // and the body starts at depth+1.
    const padLeft = d * 22;
    const isLoopMarker = isLoopBegin || isLoopEnd;
    wrap.style.cssText = `display:flex; gap:6px; align-items:center; padding-left:${padLeft}px; ` +
      (isLoopMarker ? 'background:#f0f4ff; border-left:3px solid #6a8ec7; padding-top:3px; padding-bottom:3px; border-radius:3px;' : '');
    wrap.innerHTML = `
      <span style="color:#888; font-family:ui-monospace,Consolas,monospace; font-size:.85em; min-width:1.6em; text-align:right;">${idx + 1}.</span>
      <iconify-icon class="simple-row-icon" icon="${spec.icon}" style="font-size:1.2em; min-width:1.4em; color:${isLoopMarker ? '#3a5ca8' : '#555'};"></iconify-icon>
      ${_simpleActionPicker(row.action)}
      <input type="text" class="simple-row-detail" value="${(row.detail || '').replace(/"/g, '&quot;')}" placeholder="${spec.placeholder}"${isLoopEnd ? ' disabled' : ''} style="flex:1; padding:4px 8px; font-family:inherit;${isLoopEnd ? 'background:#eee; color:#888;' : ''}">
      <button type="button" class="simple-row-up" title="上に移動" style="background:none; border:1px solid #ccd; padding:2px 6px; cursor:pointer; border-radius:4px;">↑</button>
      <button type="button" class="simple-row-down" title="下に移動" style="background:none; border:1px solid #ccd; padding:2px 6px; cursor:pointer; border-radius:4px;">↓</button>
      <button type="button" class="simple-row-insert" title="この直後に空の navigate 行を挿入" style="background:#eef8ee; border:1px solid #7ab68a; color:#196b2c; padding:2px 6px; cursor:pointer; border-radius:4px;">+</button>
      <button type="button" class="simple-row-remove" title="この step を削除" style="background:#fee; border:1px solid #c88; color:#933; padding:2px 8px; cursor:pointer; border-radius:4px;">×</button>
    `;

    const picker     = wrap.querySelector('.simple-action-picker');
    const pickerBtn  = picker.querySelector('.ap-button');
    const popover    = picker.querySelector('.ap-popover');
    const det    = wrap.querySelector('.simple-row-detail');
    const icon   = wrap.querySelector('.simple-row-icon');
    const upBtn  = wrap.querySelector('.simple-row-up');
    const dnBtn  = wrap.querySelector('.simple-row-down');
    const insBtn = wrap.querySelector('.simple-row-insert');
    const rmBtn  = wrap.querySelector('.simple-row-remove');

    // Toggle the popover. The global outside-click handler closes
    // popovers when the click falls outside any .simple-action-picker.
    pickerBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const wasHidden = popover.hidden;
      // Close any other open popover so only one is up at a time.
      document.querySelectorAll('.ap-popover').forEach(p => {
        if (p !== popover) p.hidden = true;
      });
      popover.hidden = !wasHidden;
    });
    // Item click -> set this row's action + re-render. Stop the click
    // from bubbling to the global outside-click handler (which would
    // close before we read the value).
    popover.querySelectorAll('.ap-item').forEach(item => {
      item.addEventListener('click', (ev) => {
        ev.stopPropagation();
        _simpleRows[idx].action = item.dataset.value;
        saveSimpleRows();
        // Changing to/from loop or loop_end shifts depth for every
        // subsequent row, so re-render the whole list. Re-render also
        // tears down this popover, so no explicit close needed.
        renderSimpleRows();
      });
    });
    det.addEventListener('input', () => {
      _simpleRows[idx].detail = det.value;
      saveSimpleRows();
    });
    upBtn.addEventListener('click', () => {
      if (idx === 0) return;
      [_simpleRows[idx-1], _simpleRows[idx]] = [_simpleRows[idx], _simpleRows[idx-1]];
      saveSimpleRows();
      renderSimpleRows();
    });
    dnBtn.addEventListener('click', () => {
      if (idx >= _simpleRows.length - 1) return;
      [_simpleRows[idx+1], _simpleRows[idx]] = [_simpleRows[idx], _simpleRows[idx+1]];
      saveSimpleRows();
      renderSimpleRows();
    });
    insBtn.addEventListener('click', () => {
      // Insert a fresh navigate row immediately AFTER this one so the
      // operator can pick the desired action from the dropdown without
      // having to mash ↑ a dozen times. Replaces the "add at the end
      // then move up" workflow.
      _simpleRows.splice(idx + 1, 0, { action: 'navigate', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
      // Best-effort focus on the action dropdown of the new row so the
      // operator can keyboard-pick the action immediately.
      setTimeout(() => {
        const newWrap = host.children[idx + 2]; // +1 for warns? +1 for new row
        // Resolve more robustly: find the row whose idx span matches idx+2 (= 1-based row number).
        const rows = host.querySelectorAll('.simple-row');
        const target = rows[idx + 1];
        if (target) {
          const dropdown = target.querySelector('.simple-row-action');
          if (dropdown) dropdown.focus();
        }
      }, 0);
    });
    rmBtn.addEventListener('click', () => {
      _simpleRows.splice(idx, 1);
      saveSimpleRows();
      renderSimpleRows();
    });

    host.appendChild(wrap);
  });
}

// Compile the macro rows + the URL field into a complete
// paprika-client script. Walks rows once; each row contributes one
// indented Python line. Loop rows emit `for {var} in range(N):` and
// bump the indent for subsequent rows until the matching End loop.
function compileSimpleMacroToCode(initialUrl) {
  const url = (initialUrl || '').trim() || 'about:blank';
  const { depths, warns } = _simpleComputeDepths();
  const lines = [];
  if (warns.length) {
    warns.forEach(w => lines.push(`            # WARN: ${w}`));
  }
  // Track whether each open block (loop or if) has emitted any body
  // lines yet. Empty blocks need an explicit `pass` to be valid
  // Python. The stack holds one entry per open block:
  //   {kind: 'loop'|'if', then_count, else_count, in_else}
  // Loops only use ``then_count``; ifs use both halves.
  const blockStack = [];

  function _bumpBodyCounters() {
    // A real action row contributes one body line to every block
    // currently on the stack. ifs split between then/else depending
    // on whether the matching `Else` row has been seen.
    for (let i = 0; i < blockStack.length; i++) {
      const blk = blockStack[i];
      if (blk.kind === 'if' && blk.in_else) {
        blk.else_count += 1;
      } else {
        blk.then_count += 1;
      }
    }
  }

  for (let idx = 0; idx < _simpleRows.length; idx++) {
    const row = _simpleRows[idx];
    const d = depths[idx];
    const indent = '    '.repeat(3 + d);  // 12 + 4*depth

    if (row.action === 'loop') {
      const count = parseInt(row.detail, 10) || 1;
      const varName = _simpleIterVar(d);
      lines.push(`${indent}for ${varName} in range(${count}):`);
      blockStack.push({ kind: 'loop', then_count: 0, else_count: 0, in_else: false });
      continue;
    }
    if (row.action === 'loop_end') {
      const blk = blockStack.pop();
      if (blk && blk.then_count === 0) {
        lines.push('    '.repeat(3 + d + 1) + 'pass  # empty loop body');
      }
      continue;
    }
    if (row.action === 'if_css') {
      const sel = (row.detail || '').trim();
      lines.push(`${indent}if await page.exists(${_simpleEmitLit(sel)}):`);
      blockStack.push({ kind: 'if', then_count: 0, else_count: 0, in_else: false });
      continue;
    }
    if (row.action === 'if_agent') {
      const q = (row.detail || '').trim();
      lines.push(`${indent}if await page.ask(${_simpleEmitLit(q)}, engine="qwen"):`);
      blockStack.push({ kind: 'if', then_count: 0, else_count: 0, in_else: false });
      continue;
    }
    if (row.action === 'if_else') {
      const blk = blockStack[blockStack.length - 1];
      if (!blk || blk.kind !== 'if') {
        lines.push(`${indent}# WARN: "Else" outside any If block`);
        continue;
      }
      // If the then-branch was empty, give it a `pass` before the else.
      if (blk.then_count === 0) {
        lines.push('    '.repeat(3 + d + 1) + 'pass  # empty then');
      }
      blk.in_else = true;
      lines.push(`${indent}else:`);
      continue;
    }
    if (row.action === 'if_end') {
      const blk = blockStack.pop();
      if (blk && blk.kind === 'if') {
        const inner = '    '.repeat(3 + d + 1);
        if (blk.in_else && blk.else_count === 0) {
          lines.push(inner + 'pass  # empty else');
        } else if (!blk.in_else && blk.then_count === 0) {
          lines.push(inner + 'pass  # empty if body');
        }
      }
      continue;
    }
    // Real action row.
    const spec = _simpleActionByValue(row.action);
    let line;
    try {
      line = spec.compile(row.detail || '');
    } catch (e) {
      line = `# !! compile failed for ${row.action}: ${e}`;
    }
    lines.push(indent + line);
    _bumpBodyCounters();
  }
  if (lines.length === 0) {
    lines.push('            pass  # empty macro');
  }
  return [
    `import asyncio`,
    `import paprika_client as pap`,
    `from paprika_client import async_paprika`,
    ``,
    `# connect() の引数省略 → PAPRIKA_HUB env (runner 内で自動注入) を読む。`,
    `# ローカル実行時のみ os.environ['PAPRIKA_HUB']=http://localhost:8000 を別途セット。`,
    `async def main():`,
    `    async with async_paprika.connect() as cli:`,
    `        async with cli.session(initial_url=${JSON.stringify(url)}) as page:`,
    ...lines,
    ``,
    `asyncio.run(main())`,
    ``,
  ].join('\n');
}

// Wire up the macro builder's buttons. Done after the DOM nodes
// exist (the surrounding script runs after the form HTML).
(function wireSimpleBuilder() {
  const addBtn     = document.getElementById('simpleAddRowBtn');
  const clearBtn   = document.getElementById('simpleClearBtn');
  const previewBtn = document.getElementById('simplePreviewBtn');
  const previewPre = document.getElementById('simplePreviewPre');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      _simpleRows.push({ action: 'navigate', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  const addLoopBtn = document.getElementById('simpleAddLoopBtn');
  if (addLoopBtn) {
    addLoopBtn.addEventListener('click', () => {
      // Add a Loop + End loop pair so the user can't accidentally
      // leave an unmatched marker. The body starts empty -- they
      // can drag/add rows in between.
      _simpleRows.push({ action: 'loop', detail: '5' });
      _simpleRows.push({ action: 'loop_end', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  // If (CSS) と If (Agent) の挿入ボタン -- どちらも開閉ペアを 1 セット挿入。
  // 中身は空 (= pass 1 行を Python に吐く)。ユーザーが任意で間に Else 行を
  // dropdown から差し込む。Loop と同じ階層モデルなのでネスト自由。
  const addIfCssBtn = document.getElementById('simpleAddIfCssBtn');
  if (addIfCssBtn) {
    addIfCssBtn.addEventListener('click', () => {
      _simpleRows.push({ action: 'if_css', detail: '' });
      _simpleRows.push({ action: 'if_end', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  const addIfAgentBtn = document.getElementById('simpleAddIfAgentBtn');
  if (addIfAgentBtn) {
    addIfAgentBtn.addEventListener('click', () => {
      _simpleRows.push({ action: 'if_agent', detail: '' });
      _simpleRows.push({ action: 'if_end', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (_simpleRows.length === 0) return;
      if (!confirm('macro を全削除しますか?')) return;
      _simpleRows = [];
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  if (previewBtn && previewPre) {
    previewBtn.addEventListener('click', () => {
      const url = (document.getElementById('urlInput') || {}).value || '';
      previewPre.textContent = compileSimpleMacroToCode(url);
      previewPre.style.display = (previewPre.style.display === 'none') ? 'block' : 'none';
    });
  }
})();
syncSubmitMode();

// =========================================================================
// Named Submit-form presets
// =========================================================================
//
// Presets are server-side snapshots of the Submit form so the
// operator can re-run common configurations without retyping. They
// also expose POST /presets/{name}/run for cron / external
// triggers; the dropdown above the Submit form lets a human pick
// one and inspect/edit before re-submitting.

const PRESET_LIST_URL = '/presets';
const PRESET_ONE_URL = (n) => '/presets/' + encodeURIComponent(n);
let _presetCurrentName = null;

function presetBuildPayload(name, category, description, opts) {
  // ``opts`` (optional) lets the caller force a specific execution
  // mode regardless of which radio is currently checked on the
  // Submit form. The save-preset modal uses this to honour the
  // operator's "this preset should always run as <X>" choice
  // instead of silently inheriting the current form mode.
  // Recognised opts keys:
  //   forceMode      'fetch'|'codegen-loop'|'code'|'rerun_from'
  //   rerunFromJob   job_id (or job_id/attempts/N) -- only when
  //                  forceMode === 'rerun_from'.
  //   codeOverride   inline Python to use as ``options.code`` and
  //                  ``code_script`` instead of #codeInput. Used by
  //                  the Macro mode's "save as generated code" path
  //                  so the compiled macro script gets stored
  //                  without smearing #codeInput.
  opts = opts || {};
  const url = (document.getElementById('urlInput') || {}).value || '';
  const formMode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
  // Map forceMode → ui_mode for the Submit form (so re-loading the
  // preset later puts the form back into the right tab):
  //   'codegen-loop' -> 'ai' + ai_engine='codegen'
  //   'code'         -> 'code'
  //   'rerun_from'   -> 'code'  (the snapshot lives in options.rerun_from,
  //                              not the Code textarea; we treat it as a
  //                              flavour of code-mode for ui_mode purposes)
  let mode = formMode;
  if (opts.forceMode === 'fetch') mode = 'fetch';
  else if (opts.forceMode === 'codegen-loop') mode = 'ai';
  else if (opts.forceMode === 'code') mode = 'code';
  else if (opts.forceMode === 'rerun_from') mode = 'code';
  const engine = currentAiEngine();
  const goal = (document.getElementById('goalInput') || {}).value || '';
  // codeOverride wins so the Macro mode's "save as generated code"
  // path can hand in the compiled Python without first poking
  // #codeInput.
  const code = (typeof opts.codeOverride === 'string' && opts.codeOverride.length)
    ? opts.codeOverride
    : ((document.getElementById('codeInput') || {}).value || '');
  const maxAttempts = parseInt((document.getElementById('maxAttempts') || {}).value, 10) || 3;
  const attemptTimeout = parseInt((document.getElementById('attemptTimeout') || {}).value, 10) || 86400;
  const attemptTimeoutSimple = parseInt((document.getElementById('attemptTimeoutSimple') || {}).value, 10) || 600;
  const hostDedup = !!((document.getElementById('llmHostDedup') || {}).checked);
  let options = {};
  if (mode === 'fetch') {
    // Snapshot the full fetch-options block so saved presets round-trip
    // the operator's tuning (scroll toggle, timing knobs,
    // referer / cookies_from / attach_to_job text fields, ...). Falls
    // back to the historical hardcoded defaults if the helper isn't
    // wired yet (e.g. preset rendered before the form initialised).
    options = (typeof buildFetchOptionsFromForm === 'function')
      ? buildFetchOptionsFromForm()
      : { mode: 'fetch', scroll: true };
  } else if (mode === 'ai') {
    if (engine === 'simple') {
      const compiled = compileSimpleMacroToCode(url);
      options = {
        mode: 'rerun',
        code: compiled,
        attempt_timeout_s: attemptTimeoutSimple,
      };
    } else {
      let g = goal.trim() || DEFAULT_CRAWL_GOAL;
      if (!hostDedup) {
        g += '\n\n追加ガードレール:\n  - **pap.walk(..., host_dedup=False)** を必ず指定する (既訪問URLも再クロール)';
      }
      options = {
        mode: 'codegen-loop',
        goal: g,
        max_codegen_attempts: maxAttempts,
        attempt_timeout_s: attemptTimeout,
      };
    }
  } else if (mode === 'code') {
    if (opts.forceMode === 'rerun_from') {
      // Special-case: rerun-from-job uses the same ui_mode 'code'
      // for re-loading purposes but the options snapshot points at
      // an existing job's script instead of carrying inline code.
      options = {
        mode: 'rerun',
        rerun_from: String(opts.rerunFromJob || '').trim(),
        attempt_timeout_s: attemptTimeout,
      };
    } else {
      options = {
        mode: 'rerun',
        code,
        attempt_timeout_s: attemptTimeout,
      };
    }
  }
  return {
    name,
    category: category || '',
    description: description || '',
    ui_mode: mode,
    ai_engine: engine,
    url,
    goal,
    simple_rows: (typeof _simpleRows !== 'undefined') ? _simpleRows.slice() : [],
    code_script: code,
    max_attempts: maxAttempts,
    attempt_timeout_s: attemptTimeout,
    attempt_timeout_simple_s: attemptTimeoutSimple,
    host_dedup: hostDedup,
    options,
  };
}

function presetApplyToForm(rec) {
  if (!rec) return;
  document.getElementById('urlInput').value = rec.url || '';
  const modeRadio = document.querySelector(`input[name="mode"][value="${rec.ui_mode || 'fetch'}"]`);
  if (modeRadio) modeRadio.checked = true;
  const engineRadio = document.querySelector(`input[name="aiEngine"][value="${rec.ai_engine || 'codegen'}"]`);
  if (engineRadio) engineRadio.checked = true;
  const g = document.getElementById('goalInput');  if (g) g.value = rec.goal || '';
  const c = document.getElementById('codeInput');  if (c) c.value = rec.code_script || '';
  const m = document.getElementById('maxAttempts'); if (m) m.value = rec.max_attempts || 3;
  const t = document.getElementById('attemptTimeout'); if (t) t.value = rec.attempt_timeout_s || 86400;
  const ts = document.getElementById('attemptTimeoutSimple'); if (ts) ts.value = rec.attempt_timeout_simple_s || 600;
  const dd = document.getElementById('llmHostDedup');
  if (dd) dd.checked = (rec.host_dedup === undefined ? true : !!rec.host_dedup);
  // Restore fetch-options fields from rec.options (the snapshot the
  // preset-builder captured at save time). Missing keys mean "use the
  // form default that's already there" -- don't clobber.
  const fopt = rec.options || {};
  const setChk = (id, v, dflt) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = (v === undefined) ? dflt : !!v;
  };
  const setNum = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  const setTxt = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = String(v);
  };
  if ((rec.ui_mode || 'fetch') === 'fetch') {
    setChk('fetchScroll',         fopt.scroll,          true);
    setChk('fetchDownloadVideo',  fopt.download_video,  false);
    setChk('fetchHeadless',       fopt.headless,        false);
    setChk('fetchCaptureAssets',  fopt.capture_assets,  true);
    setChk('fetchKeepSession',    fopt.keep_session,    false);
    setNum('fetchWaitSec',          fopt.wait_seconds);
    setNum('fetchIdleSec',          fopt.idle_seconds);
    setNum('fetchMaxWaitSec',       fopt.max_wait_seconds);
    setNum('fetchScrollMax',        fopt.scroll_max);
    setNum('fetchPostClickSec',     fopt.post_click_seconds);
    setNum('fetchMinAssetBytes',    fopt.min_asset_size_bytes);
    setTxt('fetchReferer',          fopt.referer || '');
    setTxt('fetchAttachToJob',      fopt.attach_to_job || '');
  }
  if (typeof _simpleRows !== 'undefined' && Array.isArray(rec.simple_rows)) {
    _simpleRows = rec.simple_rows.map(r => ({
      action: typeof r.action === 'string' ? r.action : 'navigate',
      detail: typeof r.detail === 'string' ? r.detail : '',
    }));
    if (typeof saveSimpleRows === 'function') saveSimpleRows();
  }
  syncSubmitMode();
  if (typeof renderSimpleRows === 'function') renderSimpleRows();
}

function presetSetLoaded(name) {
  _presetCurrentName = name || null;
  const lbl = document.getElementById('presetLoadedName');
  const ow  = document.getElementById('presetOverwriteBtn');
  if (lbl) {
    if (name) {
      lbl.textContent = `(loaded: ${name})`;
      lbl.style.color = '#3a5ca8';
    } else {
      lbl.textContent = '(none loaded — pick one from the Preset job tab)';
      lbl.style.color = '#888';
    }
  }
  if (ow)  ow.style.display = name ? '' : 'none';
}

// ---------------------------------------------------------------------------
// Preset-save modal (replaces the older 3x window.prompt chain).
//
// The modal lets the operator override "this preset should execute as
// <X> regardless of the Submit form's current state". Pre-modal this
// was a hidden footgun: clicking "save as" on a form that had drifted
// back to fetch silently produced a fetch-mode preset even when the
// operator's intent was "save the AI / Code workflow I just ran".
// ---------------------------------------------------------------------------
const _PRESET_MODAL = {
  open: false,
  mode: 'save-as',     // 'save-as' | 'overwrite'
  onSubmit: null,      // resolves the openPresetSaveModal() promise
};

function _presetModalSetExtraVisibility() {
  const mode = (document.querySelector('input[name="presetSaveModalMode"]:checked') || {}).value || 'inherit';
  const rerunBlock = document.getElementById('presetSaveModalRerunFromBlock');
  const codeNote = document.getElementById('presetSaveModalCodeNote');
  const cgNote = document.getElementById('presetSaveModalCodegenNote');
  if (rerunBlock) rerunBlock.style.display = (mode === 'rerun_from') ? 'flex' : 'none';
  if (codeNote)   codeNote.style.display   = (mode === 'code') ? 'block' : 'none';
  if (cgNote)     cgNote.style.display     = (mode === 'codegen-loop') ? 'block' : 'none';
}

function _presetModalFetchCategoriesInto(datalist) {
  // Best-effort category autocomplete. We swallow failures so the
  // modal stays usable even when /presets returns the malformed-JSON
  // edge case observed in prod.
  if (!datalist) return;
  fetch('/presets?limit=500').then(r => r.ok ? r.json() : null).then(d => {
    if (!d || !Array.isArray(d.categories)) return;
    datalist.innerHTML = d.categories
      .map(c => `<option value="${(c || '').replace(/"/g, '&quot;')}"></option>`)
      .join('');
  }).catch(() => {});
}

function openPresetSaveModal({
  mode = 'save-as',
  initialName = '',
  initialCategory = '',
  initialDescription = '',
  // When set, the modal opens with the rerun_from radio already
  // checked and the Job ID field pre-populated. Used by the Live
  // panel's "save preset" button so the operator doesn't have to
  // copy/paste a job ID across the UI to save a successful run.
  prefillRerunFromJob = '',
  // Optional title override (e.g. "Save this job as preset" instead
  // of the generic "Save preset"). Falls back to the mode-based
  // default when empty.
  titleOverride = '',
} = {}) {
  return new Promise(resolve => {
    const modal = document.getElementById('presetSaveModal');
    if (!modal) { resolve(null); return; }
    const titleEl = document.getElementById('presetSaveModalTitle');
    const nameEl = document.getElementById('presetSaveModalName');
    const catEl  = document.getElementById('presetSaveModalCategory');
    const descEl = document.getElementById('presetSaveModalDescription');
    const errEl  = document.getElementById('presetSaveModalErr');
    const hintEl = document.getElementById('presetSaveModalHint');
    const inheritRadio  = document.querySelector('input[name="presetSaveModalMode"][value="inherit"]');
    const rerunFromRadio = document.querySelector('input[name="presetSaveModalMode"][value="rerun_from"]');
    if (titleEl) {
      titleEl.textContent = titleOverride
        || ((mode === 'overwrite') ? 'Overwrite preset' : 'Save preset');
    }
    if (nameEl)  { nameEl.value = initialName || ''; nameEl.readOnly = (mode === 'overwrite'); }
    if (catEl)   catEl.value = initialCategory || '';
    if (descEl)  descEl.value = initialDescription || '';
    if (errEl)   errEl.textContent = '';
    // When prefillRerunFromJob is set, default the modal to the
    // rerun_from path so the operator's first action is "name it
    // and click save". Otherwise stick with the inherit default.
    if (prefillRerunFromJob && rerunFromRadio) {
      rerunFromRadio.checked = true;
    } else if (inheritRadio) {
      inheritRadio.checked = true;
    }
    document.getElementById('presetSaveModalRerunFromJob').value = prefillRerunFromJob || '';
    // Surface the current form's mode so the operator can tell at a
    // glance what "inherit" would save.
    const formMode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
    const formEngine = (document.querySelector('input[name="aiEngine"]:checked') || {}).value || '';
    let inheritLabel = formMode;
    if (formMode === 'ai') inheritLabel = `ai (engine=${formEngine || 'codegen'})`;
    if (hintEl) {
      hintEl.textContent = prefillRerunFromJob
        ? `rerun_from = ${prefillRerunFromJob}`
        : `現在のフォーム: ${inheritLabel}`;
    }
    _presetModalSetExtraVisibility();
    _presetModalFetchCategoriesInto(document.getElementById('presetSaveModalCategoryList'));
    _PRESET_MODAL.open = true;
    _PRESET_MODAL.mode = mode;
    _PRESET_MODAL.onSubmit = resolve;
    modal.style.display = 'flex';
    setTimeout(() => { if (nameEl && !nameEl.readOnly) nameEl.focus(); }, 0);
  });
}

function closePresetSaveModal(result) {
  const modal = document.getElementById('presetSaveModal');
  if (modal) modal.style.display = 'none';
  if (_PRESET_MODAL.onSubmit) {
    const r = _PRESET_MODAL.onSubmit;
    _PRESET_MODAL.onSubmit = null;
    _PRESET_MODAL.open = false;
    try { r(result); } catch (_) {}
  }
}

(function wirePresetSaveModal() {
  const modal = document.getElementById('presetSaveModal');
  if (!modal) return;
  // Wire radio change -> show/hide conditional blocks.
  document.querySelectorAll('input[name="presetSaveModalMode"]').forEach(r => {
    r.addEventListener('change', _presetModalSetExtraVisibility);
  });
  const closeBtn  = document.getElementById('presetSaveModalClose');
  const cancelBtn = document.getElementById('presetSaveModalCancel');
  const saveBtn   = document.getElementById('presetSaveModalSave');
  if (closeBtn)  closeBtn.addEventListener('click', () => closePresetSaveModal(null));
  if (cancelBtn) cancelBtn.addEventListener('click', () => closePresetSaveModal(null));
  // Backdrop click closes too (but only when clicking the overlay itself).
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closePresetSaveModal(null);
  });
  // Esc closes.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _PRESET_MODAL.open) closePresetSaveModal(null);
  });
  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      const name = (document.getElementById('presetSaveModalName').value || '').trim();
      const category = (document.getElementById('presetSaveModalCategory').value || '').trim();
      const description = (document.getElementById('presetSaveModalDescription').value || '').trim();
      const mode = (document.querySelector('input[name="presetSaveModalMode"]:checked') || {}).value || 'inherit';
      const errEl = document.getElementById('presetSaveModalErr');
      const setErr = (msg) => { if (errEl) errEl.textContent = msg || ''; };
      setErr('');
      if (!name) { setErr('Name は必須です'); return; }
      let forceMode = null;
      let rerunFromJob = '';
      if (mode === 'fetch')         forceMode = 'fetch';
      else if (mode === 'codegen-loop') forceMode = 'codegen-loop';
      else if (mode === 'code')     forceMode = 'code';
      else if (mode === 'rerun_from') {
        forceMode = 'rerun_from';
        rerunFromJob = (document.getElementById('presetSaveModalRerunFromJob').value || '').trim();
        if (!rerunFromJob) { setErr('rerun_from モードでは Job ID が必須です'); return; }
      }
      closePresetSaveModal({ name, category, description, forceMode, rerunFromJob });
    });
  }
})();

(function wirePresetBar() {
  // The dropdown selector was removed because operators can have
  // 500+ presets; picking is now done from the Preset job tab.
  // We keep "save as" / "overwrite" on the Submit form since
  // those operate on the LIVE form state, not on a saved record.
  // ---- Shared save flow ------------------------------------------------
  //
  // Each entry point (Fetch / Code direct save, LLM dropdown items,
  // Macro dropdown items) decides WHAT it's saving and calls this
  // helper, which opens the simplified save modal (name / category /
  // description) and PUTs the result. The modal's old in-modal
  // mode-picker still works for callers that don't pre-decide; the
  // new flows skip that picker by passing forceMode / codeOverride
  // up front.
  async function _runSaveFlow({ forceMode, codeOverride, rerunFromJob, titleOverride, defaultName }) {
    const res = await openPresetSaveModal({
      mode: 'save-as',
      initialName: defaultName || '',
      titleOverride: titleOverride || '',
      prefillRerunFromJob: (forceMode === 'rerun_from') ? (rerunFromJob || '') : '',
    });
    if (!res) return;
    const finalForceMode = forceMode || res.forceMode;
    const finalRerunFromJob = (finalForceMode === 'rerun_from') ? (rerunFromJob || res.rerunFromJob) : '';
    const payload = presetBuildPayload(
      res.name, res.category, res.description,
      { forceMode: finalForceMode, rerunFromJob: finalRerunFromJob, codeOverride },
    );
    try {
      const r = await fetch(PRESET_ONE_URL(res.name), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        alert(`Save failed (HTTP ${r.status}): ${await r.text()}`);
        return;
      }
      presetSetLoaded(res.name);
      if (typeof renderPresets === 'function') renderPresets();
    } catch (e) {
      alert(`Save failed: ${e}`);
    }
  }

  // ---- Per-mode dropdown menu (LLM / Macro only) -----------------------
  //
  // Fetch and Code each have exactly one thing worth saving, so we
  // skip the menu and open the modal directly. LLM and Macro each
  // have TWO meaningful save types (process recipe vs frozen
  // generated code), so for those we pop a small menu below the
  // save button.
  let _savePopdown = null;
  function _closeSavePopdown() {
    if (_savePopdown) { _savePopdown.remove(); _savePopdown = null; }
  }
  function _openSavePopdown(anchor, items) {
    _closeSavePopdown();
    const rect = anchor.getBoundingClientRect();
    const pop = document.createElement('div');
    pop.id = 'presetSaveDropdown';
    pop.style.cssText = `
      position: fixed; z-index: 1100;
      left: ${Math.round(rect.left)}px; top: ${Math.round(rect.bottom + 4)}px;
      background: #fff; border: 1px solid #ccd; border-radius: 6px;
      box-shadow: 0 6px 18px rgba(0,0,0,.15);
      padding: 4px; min-width: 280px;
      display: flex; flex-direction: column; gap: 2px;
    `;
    for (const it of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.disabled = !!it.disabled;
      btn.style.cssText = `
        text-align: left; padding: 8px 10px; border: none; background: transparent;
        border-radius: 4px; cursor: ${it.disabled ? 'not-allowed' : 'pointer'};
        font-size: .9em; color: ${it.disabled ? '#aaa' : '#222'};
      `;
      btn.innerHTML = `
        <div style="font-weight:600;">${it.icon || ''} ${it.label}</div>
        <div style="color:#888; font-size:.82em; font-weight:400; margin-top:2px;">${it.hint || ''}</div>
      `;
      btn.addEventListener('mouseenter', () => { if (!it.disabled) btn.style.background = '#f5f5fa'; });
      btn.addEventListener('mouseleave', () => { btn.style.background = 'transparent'; });
      btn.addEventListener('click', () => {
        _closeSavePopdown();
        if (!it.disabled && it.onClick) it.onClick();
      });
      pop.appendChild(btn);
    }
    document.body.appendChild(pop);
    _savePopdown = pop;
    // Dismiss on outside click / Esc.
    setTimeout(() => {
      const onDoc = (e) => {
        if (!pop.contains(e.target) && e.target !== anchor) {
          _closeSavePopdown();
          document.removeEventListener('mousedown', onDoc, true);
        }
      };
      document.addEventListener('mousedown', onDoc, true);
    }, 0);
  }
  function _currentLiveLlmJobId() {
    // Live panel's currently-attached job, IFF it ran in codegen-loop
    // mode (= a real LLM-generated script exists at /jobs/{id}/script.py).
    // Used by the "save generated code" dropdown item.
    if (typeof LJP === 'undefined') return null;
    if (!LJP.jobId) return null;
    if (LJP.mode !== 'codegen-loop' && LJP.mode !== 'rerun') return null;
    return LJP.jobId;
  }

  const saveBtn = document.getElementById('presetSaveAsBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      // Dispatch by current Submit-form mode.
      const formMode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
      const aiEngine = currentAiEngine();
      if (formMode === 'fetch') {
        // One-shot save: opens the modal already decided.
        return _runSaveFlow({ forceMode: 'fetch', titleOverride: 'Save Fetch preset' });
      }
      if (formMode === 'code') {
        return _runSaveFlow({ forceMode: 'code', titleOverride: 'Save Code preset' });
      }
      if (formMode === 'ai' && aiEngine === 'codegen') {
        // LLM dropdown: 2 options.
        const liveJid = _currentLiveLlmJobId();
        _openSavePopdown(saveBtn, [
          {
            icon: '🎯',
            label: 'Goal を保存',
            hint: '実行ごとに LLM が新しいスクリプトを生成する設定 (Goal + 試行設定 + コード生成 LLM)',
            onClick: () => _runSaveFlow({
              forceMode: 'codegen-loop',
              titleOverride: 'Save Goal preset',
            }),
          },
          {
            icon: '📜',
            label: '生成コードを保存',
            hint: liveJid
              ? `Live ジョブ ${liveJid} の最終 script を固定保存 (LLM 呼ばずに同じスクリプトを再実行)`
              : '※ Live パネルに codegen-loop のジョブを開いてから利用してください',
            disabled: !liveJid,
            onClick: () => _runSaveFlow({
              forceMode: 'rerun_from',
              rerunFromJob: liveJid,
              titleOverride: `Save generated script from ${liveJid}`,
            }),
          },
        ]);
        return;
      }
      if (formMode === 'ai' && aiEngine === 'simple') {
        // Macro dropdown: 2 options.
        const urlVal = (document.getElementById('urlInput') || {}).value || '';
        const compiled = (typeof compileSimpleMacroToCode === 'function')
          ? compileSimpleMacroToCode(urlVal)
          : '';
        const hasRows = (typeof _simpleRows !== 'undefined') && _simpleRows && _simpleRows.length > 0;
        _openSavePopdown(saveBtn, [
          {
            icon: '⠿',
            label: 'Macro を保存',
            hint: '行構成 + コンパイル済み Python を一緒に保存 (後で Macro UI で再編集可)',
            disabled: !hasRows,
            onClick: () => _runSaveFlow({
              // inherit current form (= mode=ai + engine=simple) so
              // simple_rows + compiled code both round-trip.
              titleOverride: 'Save Macro preset',
            }),
          },
          {
            icon: '📜',
            label: '生成コードを保存',
            hint: 'コンパイル済み Python のみ保存 (Macro UI 復元不可、Code として再編集可)',
            disabled: !compiled,
            onClick: () => _runSaveFlow({
              forceMode: 'code',
              codeOverride: compiled,
              titleOverride: 'Save Macro compiled code',
            }),
          },
        ]);
        return;
      }
      // Fallback (shouldn't reach here but keeps old behaviour).
      _runSaveFlow({});
    });
  }
  const owBtn = document.getElementById('presetOverwriteBtn');
  if (owBtn) {
    owBtn.addEventListener('click', async () => {
      if (!_presetCurrentName) return;
      // Fetch current snapshot so the modal can prefill cat / desc.
      let oldCat = '', oldDesc = '';
      try {
        const r0 = await fetch(PRESET_ONE_URL(_presetCurrentName));
        if (r0.ok) {
          const old = await r0.json();
          oldCat = old.category || '';
          oldDesc = old.description || '';
        }
      } catch (_) {}
      const res = await openPresetSaveModal({
        mode: 'overwrite',
        initialName: _presetCurrentName,
        initialCategory: oldCat,
        initialDescription: oldDesc,
      });
      if (!res) return;
      const { name, category, description, forceMode, rerunFromJob } = res;
      const payload = presetBuildPayload(name, category, description, { forceMode, rerunFromJob });
      try {
        const r = await fetch(PRESET_ONE_URL(_presetCurrentName), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!r.ok) {
          const err = await r.text();
          alert(`Overwrite failed (HTTP ${r.status}): ${err}`);
          return;
        }
        if (typeof renderPresets === 'function') renderPresets();
      } catch (e) {
        alert(`Overwrite failed: ${e}`);
      }
    });
  }
})();

// Make Tab in the Code textarea insert 4 spaces instead of moving focus.
// Without this, pasting Python in and trying to fix indentation is painful.
(function () {
  const ta = document.getElementById('codeInput');
  if (!ta) return;
  ta.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab' || e.shiftKey) return;
    e.preventDefault();
    const s = ta.selectionStart, en = ta.selectionEnd;
    ta.value = ta.value.substring(0, s) + '    ' + ta.value.substring(en);
    ta.selectionStart = ta.selectionEnd = s + 4;
  });
  // "Insert template" button -- only overwrites when textarea is empty,
  // so accidental clicks don't nuke in-progress code.
  const tplBtn = document.getElementById('codeLoadTemplate');
  if (tplBtn) tplBtn.addEventListener('click', () => {
    if (ta.value.trim() && !confirm('Overwrite the current code with the template?')) return;
    ta.value =
`import asyncio
import paprika_client as pap
from paprika_client import async_paprika

# connect() の引数省略 → PAPRIKA_HUB env (runner で自動注入される
# http://hub:8000) を SDK が読む。ローカル実行時のみ
# os.environ['PAPRIKA_HUB']=http://localhost:8000 を別途セット。

async def main():
    async with async_paprika.connect() as cli:
        async with cli.session(initial_url='https://example.com/') as page:
            # Clear any startup modal FIRST (age gate / consent dialog).
            await page.agent(
                'If an age verification or consent dialog appears, '
                'accept it. Otherwise return done immediately.',
                max_steps=2,
            )

            # BFS-walk the site, persisting state so retries resume.
            async for visit in pap.walk(
                page,
                target_pages=20,
                same_domain=True,
            ):
                print(f'[{visit.n}/{visit.target}] {visit.url}')
                # Optionally download a video on video pages:
                # if '/video' in visit.url:
                #     r = await page.download_video(timeout_s=600)
                #     print(f'  downloaded {r["file_count"]} file(s)')

asyncio.run(main())
`;
    ta.focus();
  });
})();


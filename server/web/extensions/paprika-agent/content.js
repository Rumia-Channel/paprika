// Paprika Agent -- content script relay (page <-> service worker).
//
// The worker can't reliably attach to the (dormant) MV3 service worker
// over CDP, but it CAN evaluate JS in the page. So the worker posts a
// command on the page's window; this content script (injected into the
// page) forwards it to the service worker via chrome.runtime.sendMessage
// -- which wakes the worker -- and posts the response back on the
// window, where the worker's evaluated promise is waiting.
//
// Protocol (all on window.postMessage, same-window only):
//   request : { __paprikaAgentReq: "<id>", cmd: "...", args: {...} }
//   response: { __paprikaAgentResp: "<id>", ok, result?, error? }

window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (!d || typeof d.__paprikaAgentReq !== "string") return;
  const reqId = d.__paprikaAgentReq;
  try {
    chrome.runtime.sendMessage(
      { __paprikaAgent: true, cmd: d.cmd, args: d.args },
      (resp) => {
        const err = chrome.runtime.lastError;
        window.postMessage(
          {
            __paprikaAgentResp: reqId,
            ok: !err && !!(resp && resp.ok),
            result: resp ? resp.result : undefined,
            error: err ? err.message : (resp ? resp.error : "no response"),
          },
          "*",
        );
      },
    );
  } catch (e) {
    window.postMessage(
      { __paprikaAgentResp: reqId, ok: false, error: String((e && e.message) || e) },
      "*",
    );
  }
});

// ----- Operator-event logger (programming-by-demonstration MVP1) ------
// When recording is active (toggled via the agent command bus from the
// hub / admin UI), capture the operator's manual interactions in noVNC
// -- clicks, form inputs, key presses on inputs, submits, navigations
// -- and forward them to the service worker for buffering. The worker
// drains the buffer with getOperatorEvents. Future phases will verbalise
// (Qwen3-VL on the surrounding-pixel crop) and store as host learnings.
//
// Privacy:
//   * password fields: value is NEVER captured (only length + a redaction
//     marker), regardless of recording state.
//   * other inputs: first 40 chars of value, length, type.
//   * targets: outerHTML capped at 200, innerText at 120.
//
// State location: a single boolean lives in chrome.storage.session under
// "opRecording" (see background.js). Content script polls it via SW
// message at most once per second to keep the listener path cheap.
(function setupOperatorEventLogger() {
  let _recordingCached = false;
  let _recordingCacheT = 0;
  function _isRecording(cb) {
    const now = Date.now();
    // Cheap cache so high-frequency events (scroll/move/keystroke) don't
    // hammer the SW with state queries.
    if (now - _recordingCacheT < 1000) { cb(_recordingCached); return; }
    try {
      chrome.runtime.sendMessage(
        { __paprikaAgent: true, cmd: "recordingState", args: {} },
        (resp) => {
          const ok = resp && resp.ok && resp.result && !!resp.result.active;
          _recordingCached = !!ok;
          _recordingCacheT = Date.now();
          cb(_recordingCached);
        },
      );
    } catch (_e) {
      cb(false);
    }
  }

  function _cssPath(el) {
    // Short, mostly-stable selector path. Not bullet-proof against
    // shadow DOM / dynamic classnames, but enough for an MVP that's
    // about to be verbalised by an LLM anyway.
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      let part = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift(part + "#" + CSS.escape(cur.id)); break; }
      if (cur.classList && cur.classList.length) {
        const cls = [...cur.classList].slice(0, 2).map(CSS.escape).join(".");
        if (cls) part += "." + cls;
      }
      const parent = cur.parentElement;
      if (parent) {
        const siblings = [...parent.children].filter(
          (c) => c.tagName === cur.tagName,
        );
        if (siblings.length > 1) {
          const idx = siblings.indexOf(cur) + 1;
          part += `:nth-of-type(${idx})`;
        }
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(" > ");
  }

  function _describeTarget(el) {
    if (!el || el.nodeType !== 1) return null;
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    const attrs = {};
    try {
      for (const a of el.attributes || []) {
        if (a.name === "style") continue;
        attrs[a.name] = String(a.value).slice(0, 200);
      }
    } catch (_e) {}
    let outer = "";
    try { outer = (el.outerHTML || "").slice(0, 200); } catch (_e) {}
    let text = "";
    try { text = (el.innerText || el.textContent || "").trim().slice(0, 120); } catch (_e) {}
    return {
      tag: el.tagName ? el.tagName.toLowerCase() : "",
      role: el.getAttribute ? (el.getAttribute("role") || "") : "",
      id: el.id || "",
      name: el.getAttribute ? (el.getAttribute("name") || "") : "",
      type: el.getAttribute ? (el.getAttribute("type") || "") : "",
      classes: el.classList ? [...el.classList].slice(0, 6) : [],
      text,
      outer,
      attrs,
      selector: _cssPath(el),
      bbox: rect ? {
        x: Math.round(rect.left), y: Math.round(rect.top),
        w: Math.round(rect.width), h: Math.round(rect.height),
      } : null,
    };
  }

  function _isPasswordField(el) {
    try {
      if (!el || el.tagName !== "INPUT") return false;
      const t = (el.type || "").toLowerCase();
      if (t === "password") return true;
      const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
      if (ac.includes("password") || ac.includes("current-password")) return true;
      const nm = (el.getAttribute("name") || "").toLowerCase();
      if (/pass(word|wd)?|secret/.test(nm)) return true;
      return false;
    } catch (_) { return false; }
  }

  // ----- Pre-click screenshot pre-fetch (M0.5++ for visual fidelity) -
  // The "captureVisibleTab on click" path is racy: by the time the SW
  // resolves the capture, the click handler has already triggered a
  // visual change (player loading, navigation, modal open). To capture
  // what the operator ACTUALLY SAW at the moment they decided to
  // click, fire the screenshot ASAP from mousedown (which runs ~10-100
  // ms before click). The background SW stores the result keyed by an
  // id and the click handler hands the same id along so the buffered
  // clip lands on the right event.
  let _lastMousedown = null;
  document.addEventListener("mousedown", (e) => {
    if (!e.isTrusted) return;
    _isRecording((on) => {
      if (!on) return;
      const id = "md-" + Date.now() + "-" + e.clientX + "-" + e.clientY;
      _lastMousedown = {
        id,
        t: Date.now(),
        x: e.clientX,
        y: e.clientY,
      };
      try {
        chrome.runtime.sendMessage(
          {
            __paprikaAgent: true,
            cmd: "prefetchClipForCursor",
            args: { id, x: e.clientX, y: e.clientY },
          },
          () => {},
        );
      } catch (_e) {}
    });
  }, true);

  // When true, ask the service worker to grab a visible-tab JPEG and
  // crop it to the target's bbox before stashing the event. Skipped on
  // password-redacted change events (the surrounding pixels can still
  // reveal what was typed if the form rendered the value somewhere
  // else on the page).
  function _emit(type, payload, opts) {
    const ev = {
      t: Date.now(),
      type,
      url: location.href,
      viewport: { w: window.innerWidth, h: window.innerHeight,
                  sx: window.scrollX, sy: window.scrollY },
      ...payload,
    };
    const captureClip = !!(opts && opts.captureClip)
      && !payload.redacted;
    // Link click events back to the mousedown that preceded them, so
    // the SW can attach the pre-click screenshot it pre-fetched.
    // Only valid if the mousedown was very recent (<500ms) and matches
    // the click roughly in cursor position.
    let prefetchId = null;
    if (captureClip && type === "click" && _lastMousedown) {
      const ageMs = Date.now() - _lastMousedown.t;
      const cx = payload.cursor && payload.cursor.x;
      const cy = payload.cursor && payload.cursor.y;
      if (
        ageMs <= 500
        && Math.abs((cx || 0) - _lastMousedown.x) <= 4
        && Math.abs((cy || 0) - _lastMousedown.y) <= 4
      ) {
        prefetchId = _lastMousedown.id;
      }
    }
    try {
      chrome.runtime.sendMessage(
        {
          __paprikaAgent: true,
          cmd: "pushOperatorEvent",
          args: {
            ...ev,
            __captureClip: captureClip,
            __prefetchId: prefetchId,
          },
        },
        () => {},
      );
    } catch (_e) {}
  }

  // ----- click ---------------------------------------------------------
  // Capture-phase so we see the click even if the page calls
  // preventDefault / stopPropagation. Ignore synthetic ones (isTrusted
  // false) so we don't loop on programmatic clicks fired by our own
  // codegen scripts.
  document.addEventListener("click", (e) => {
    if (!e.isTrusted) return;
    _isRecording((on) => {
      if (!on) return;
      const tgt = _describeTarget(e.target);
      if (!tgt) return;
      _emit("click", {
        target: tgt,
        button: e.button,
        modifiers: {
          alt: !!e.altKey, ctrl: !!e.ctrlKey,
          shift: !!e.shiftKey, meta: !!e.metaKey,
        },
        cursor: { x: e.clientX, y: e.clientY },
      }, { captureClip: true });
    });
  }, true);

  // ----- input (change) -----------------------------------------------
  // 'change' fires once per "commit" (blur on text, click on checkbox /
  // radio / select). That's a much cleaner signal than 'input' for
  // record-then-replay, and it avoids per-keystroke spam.
  document.addEventListener("change", (e) => {
    if (!e.isTrusted) return;
    _isRecording((on) => {
      if (!on) return;
      const el = e.target;
      const tgt = _describeTarget(el);
      if (!tgt) return;
      let value = null;
      let redacted = false;
      try {
        if (_isPasswordField(el)) {
          redacted = true;
          value = null;
        } else if (el.type === "checkbox" || el.type === "radio") {
          value = !!el.checked;
        } else if (el.tagName === "SELECT") {
          value = String(el.value || "").slice(0, 40);
        } else {
          value = String(el.value || "").slice(0, 40);
        }
      } catch (_e) {}
      _emit("change", {
        target: tgt,
        value,
        value_length: (el.value != null ? String(el.value).length : null),
        redacted,
      }, { captureClip: true });
    });
  }, true);

  // ----- key press on inputs (Enter / Esc only) ------------------------
  // Enter = "submit current line" intent, Esc = "cancel". Capturing every
  // keydown would flood the buffer; these two are the load-bearing ones.
  document.addEventListener("keydown", (e) => {
    if (!e.isTrusted) return;
    if (e.key !== "Enter" && e.key !== "Escape") return;
    _isRecording((on) => {
      if (!on) return;
      const tgt = _describeTarget(e.target);
      _emit("keydown", {
        target: tgt,
        key: e.key,
        modifiers: {
          alt: !!e.altKey, ctrl: !!e.ctrlKey,
          shift: !!e.shiftKey, meta: !!e.metaKey,
        },
      });
    });
  }, true);

  // ----- form submit ---------------------------------------------------
  document.addEventListener("submit", (e) => {
    if (!e.isTrusted) return;
    _isRecording((on) => {
      if (!on) return;
      const f = e.target;
      _emit("submit", {
        target: _describeTarget(f),
        action: (f && f.action) || "",
        method: ((f && f.method) || "get").toLowerCase(),
      });
    });
  }, true);

  // ----- page nav ------------------------------------------------------
  // Best-effort: programmatic SPA navigations don't fire beforeunload.
  // We rely on background's webNavigation listener for the canonical
  // nav log; this one captures the "operator clicked link, page about
  // to leave" signal so the buffer has a marker.
  window.addEventListener("beforeunload", () => {
    // Fire-and-forget; SW may be dormant but the event still queues.
    _isRecording((on) => {
      if (!on) return;
      _emit("unload", {});
    });
  });
})();

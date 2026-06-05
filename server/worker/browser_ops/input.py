"""Keyboard/selector input primitives (click/fill/press/type/scroll). (browser_ops package; see _base.py for shared helpers)."""

from __future__ import annotations
import asyncio
import base64
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from nodriver import cdp

from ._base import *  # noqa: F401,F403
from ._base import ACTION_SETTLE_S, LogFn, normalize_selector, short_error

async def click(tab, selector: str, log: LogFn) -> str:
    if not selector:
        return "ERR: empty selector"
    rewritten = normalize_selector(selector)
    if rewritten != selector:
        log(f"  [agent] click: rewrote {selector!r} -> {rewritten!r}")
        selector = rewritten
    # querySelector is the source of truth -- it handles the
    # [data-paprika-id="N"] form we lean on, and it's the same matcher
    # the LLM is asked to think in.
    js = (
        "(()=>{try{const el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return 'NO_MATCH'; el.click(); return 'OK';}"
        "catch(e){return 'ERR: '+e.message;}})()"
    )
    try:
        result = await tab.evaluate(js)
    except Exception as e:
        result = f"ERR: {short_error(e)}"
    if result != "OK":
        log(f"  [agent] click {selector!r}: {result}")
    await asyncio.sleep(ACTION_SETTLE_S)
    return result


async def fill(tab, selector: str, value: str, log: LogFn) -> str:
    """Set the value of an ``<input>``/``<textarea>``/contenteditable.

    Playwright's ``page.fill(selector, value)`` shape -- one call sets
    the value and fires ``input``/``change`` events. For per-character
    typing semantics use ``press_key`` in a loop.
    """
    if not selector:
        return "ERR: empty selector"
    rewritten = normalize_selector(selector)
    if rewritten != selector:
        log(f"  [agent] fill: rewrote {selector!r} -> {rewritten!r}")
        selector = rewritten
    js = (
        "(()=>{try{const el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return 'NO_MATCH'; el.focus();"
        "if('value' in el){el.value=" + json.dumps(value) + ";}"
        "else{el.innerText=" + json.dumps(value) + ";}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "return 'OK';}catch(e){return 'ERR: '+e.message;}})()"
    )
    try:
        result = await tab.evaluate(js)
    except Exception as e:
        result = f"ERR: {short_error(e)}"
    if result != "OK":
        log(f"  [agent] fill {selector!r}: {result}")
    await asyncio.sleep(ACTION_SETTLE_S)
    return result


_SPECIAL_KEY_CODES: dict[str, tuple[str, int]] = {
    "Backspace": ("Backspace", 8),
    "Tab": ("Tab", 9),
    "Enter": ("Enter", 13),
    "Return": ("Enter", 13),
    "Shift": ("ShiftLeft", 16),
    "Control": ("ControlLeft", 17),
    "Alt": ("AltLeft", 18),
    "Pause": ("Pause", 19),
    "CapsLock": ("CapsLock", 20),
    "Escape": ("Escape", 27),
    " ": ("Space", 32),
    "Space": ("Space", 32),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "End": ("End", 35),
    "Home": ("Home", 36),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39),
    "ArrowDown": ("ArrowDown", 40),
    "Insert": ("Insert", 45),
    "Delete": ("Delete", 46),
    "Meta": ("MetaLeft", 91),
    "F1": ("F1", 112),
    "F2": ("F2", 113),
    "F3": ("F3", 114),
    "F4": ("F4", 115),
    "F5": ("F5", 116),
    "F6": ("F6", 117),
    "F7": ("F7", 118),
    "F8": ("F8", 119),
    "F9": ("F9", 120),
    "F10": ("F10", 121),
    "F11": ("F11", 122),
    "F12": ("F12", 123),
}


def _resolve_key_payload(key: str) -> dict:
    """Build the CDP dispatch_key_event kwargs for ``key``.

    Returns a dict with ``key`` + (when applicable) ``code`` and
    ``windows_virtual_key_code``. The caller adds type_/modifiers.
    Unknown keys fall through with just ``key`` set; that matches
    the prior behaviour for arbitrary strings.
    """
    if not key:
        return {}
    # Single ASCII letter -> KeyA / KeyB / ... + keycode (65-90).
    if len(key) == 1 and key.isascii() and key.isalpha():
        upper = key.upper()
        return {
            "key": key,
            "code": f"Key{upper}",
            "windows_virtual_key_code": ord(upper),
        }
    # Single ASCII digit -> Digit0 / Digit1 / ... + keycode (48-57).
    if len(key) == 1 and key.isascii() and key.isdigit():
        return {
            "key": key,
            "code": f"Digit{key}",
            "windows_virtual_key_code": ord(key),
        }
    # Special key table.
    special = _SPECIAL_KEY_CODES.get(key)
    if special:
        code, kcode = special
        return {
            "key": key,
            "code": code,
            "windows_virtual_key_code": kcode,
        }
    # Anything else: just pass the raw key string. Chrome will do its
    # best; most one-shot symbol keys (``"+"``, ``"."``, etc.) work
    # via insertText / dispatch_key_event with key alone.
    return {"key": key}


_MODIFIER_BITS = {
    "alt": 1,
    "option": 1,
    "opt": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "command": 4,
    "win": 4,
    "super": 4,
    "shift": 8,
}


def _parse_key_combo(key: str) -> tuple[str, int]:
    """Split a combo string like ``"Ctrl+Shift+A"`` into ``("A", 2|8)``.

    Plain key strings (``"Enter"``, ``"a"``, ``"Backspace"``) come back
    unchanged with modifiers=0. Unknown modifier names are silently
    ignored (we'd rather press the bare key than refuse). Trailing /
    leading whitespace is tolerated.
    """
    if not key:
        return ("", 0)
    parts = [p.strip() for p in key.split("+") if p.strip()]
    if not parts:
        return ("", 0)
    *mod_parts, real_key = parts
    bits = 0
    for m in mod_parts:
        bits |= _MODIFIER_BITS.get(m.lower(), 0)
    return (real_key, bits)


async def press_key(
    tab,
    key: str,
    log: LogFn,
    *,
    count: int = 1,
    modifiers: int | None = None,
    inter_press_delay_s: float = 0.05,
) -> str:
    """Dispatch CDP keyDown+keyUp pairs.

    ``key`` accepts either a plain W3C key name (``"Enter"``, ``"Tab"``,
    ``"ArrowDown"``, ``"Backspace"``) or a combo string like
    ``"Ctrl+A"`` / ``"Ctrl+Shift+T"``. When ``modifiers`` is also
    provided explicitly it OR's with anything parsed from the combo
    string.

    ``count`` repeats the keyDown+keyUp pair N times with
    ``inter_press_delay_s`` between repeats (default 50ms -- short
    enough that the page feels a "rapid" sequence, long enough that
    auto-repeat-suppressing scripts still notice each press).
    A single ``ACTION_SETTLE_S`` wait runs once at the end so the
    overall settle behaviour matches the rest of browser_ops.
    """
    if not key:
        return "ERR: empty key"
    real_key, combo_bits = _parse_key_combo(key)
    if not real_key:
        return "ERR: empty key after combo parse"
    bits = (modifiers or 0) | combo_bits
    n = max(1, int(count))
    base_payload = _resolve_key_payload(real_key)
    try:
        for i in range(n):
            kwargs: dict = {"type_": "keyDown", **base_payload}
            if bits:
                kwargs["modifiers"] = bits
            await tab.send(cdp.input_.dispatch_key_event(**kwargs))
            kwargs["type_"] = "keyUp"
            await tab.send(cdp.input_.dispatch_key_event(**kwargs))
            if i + 1 < n and inter_press_delay_s > 0:
                await asyncio.sleep(inter_press_delay_s)
    except Exception as e:
        log(f"  [agent] press_key {key!r} (x{n}, modifiers={bits}): {e}")
        await asyncio.sleep(ACTION_SETTLE_S)
        return f"ERR: {e}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


async def type_text(tab, text: str, log: LogFn) -> str:
    """Insert ``text`` into whatever is currently focused.

    Uses ``Input.insertText`` (CDP) which is the "paste a string"
    primitive: it fires the same ``input`` / ``change`` events the page
    would see from real typing without simulating per-character
    keyDowns. Works for ``<input>``, ``<textarea>``, contenteditable,
    and even some canvas-based editors. Does NOT change focus -- click
    the target first if needed.

    Faster + safer than per-character ``press_key`` loops because it
    doesn't have to map every character to a virtual key code (which
    is unreliable for non-ASCII text, dead keys, IME composition, etc.).
    """
    if not text:
        return "ERR: empty text"
    try:
        await tab.send(cdp.input_.insert_text(text=text))
    except Exception as e:
        log(f"  [agent] type_text ({len(text)} chars): {e}")
        await asyncio.sleep(ACTION_SETTLE_S)
        return f"ERR: {e}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


async def scroll(tab, direction: str, pixels: int, log: LogFn) -> str:
    """Scroll the current viewport by ``pixels`` in ``direction``
    (``"up"``/``"down"``/``"left"``/``"right"``). Unknown directions
    are treated as ``"down"``.
    """
    dx, dy = 0, 0
    if direction == "down":
        dy = pixels
    elif direction == "up":
        dy = -pixels
    elif direction == "right":
        dx = pixels
    elif direction == "left":
        dx = -pixels
    js = f"window.scrollBy({dx}, {dy}); 'OK'"
    try:
        await tab.evaluate(js)
    except Exception as e:
        log(f"  [agent] scroll {direction} {pixels}: {e}")
        await asyncio.sleep(ACTION_SETTLE_S)
        return f"ERR: {e}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


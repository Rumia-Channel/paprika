"""Shared low-level helpers, consts, Snapshot for the browser_ops package."""

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

LogFn = Callable[[str], None]


@dataclass
class Snapshot:
    """One operator-visible capture saved during the agent loop."""

    label: str
    step: int
    url: str
    # Filenames under assets_dir/<label>/
    html_name: str
    png_name: str
    axtree_name: str


ACTION_SETTLE_S = float(os.environ.get("AGENT_ACTION_SETTLE_S", "1.5"))


NAVIGATION_SETTLE_S = float(os.environ.get("AGENT_NAVIGATION_SETTLE_S", "3.0"))


MAX_AX_TREE_CHARS = int(os.environ.get("AGENT_MAX_AX_TREE_CHARS", "8000"))


MAX_OUTLINE_ITEMS = int(os.environ.get("AGENT_MAX_OUTLINE_ITEMS", "60"))


TAB_HOOKS_ENABLED = os.environ.get("AGENT_TAB_HOOKS", "1") not in ("0", "false", "no")


INCLUDE_BODY_TEXT = os.environ.get("AGENT_INCLUDE_BODY_TEXT", "1") not in ("0", "false", "no")


def canon_url(url: str) -> str:
    """Normalise a URL for visited-set comparison.

    Strips the fragment (same page from the agent's point of view) and
    folds a missing trailing slash on bare hosts. Keeps query parameters
    since ``?id=1`` vs ``?id=2`` are different documents.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url.strip())
    except Exception:
        return url.strip()
    # `https://example.com` and `https://example.com/` are the same page.
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def href_in_visited(href: str, visited: set) -> bool:
    if not href or not visited:
        return False
    return canon_url(href) in visited


_BRACKET_ID_RE = re.compile(r"^\s*\[@(\d+)\]\s*$")


def normalize_selector(selector: str) -> str:
    """Models routinely echo back the outline label ``[@N]`` as if it
    were a selector. Rewrite that to the actual
    ``[data-paprika-id="N"]`` form so a cosmetic mismatch doesn't cost
    us every click.
    """
    m = _BRACKET_ID_RE.match(selector or "")
    if m:
        return f'[data-paprika-id="{m.group(1)}"]'
    return selector


def short_error(e: BaseException) -> str:
    """CDP raises with a giant ExceptionDetails repr that's useless in
    history. Pull out the human-readable bit when we can.
    """
    s = str(e)
    msg_start = s.find("Failed to execute")
    if msg_start != -1:
        end = s.find("\\n", msg_start)
        return s[msg_start : end if end != -1 else msg_start + 200]
    msg_start = s.find("description=")
    if msg_start != -1:
        q1 = s.find('"', msg_start)
        q2 = s.find('"', q1 + 1)
        if q1 != -1 and q2 != -1:
            return s[q1 + 1 : q2]
    return s[:200]


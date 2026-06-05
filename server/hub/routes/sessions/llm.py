"""LLM surfaces: ask/extract/observe/agent/codegen/solve_cloudflare.

Part of the sessions/ package; shared bits in _base.py."""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re as _re
from datetime import datetime
from pathlib import Path
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from server.hub._state import config, get_storage_dir, state
from server.hub._helpers import _asset_upload_url
from server.hub.codegen import (
    CODEGEN_LLM_URL,
    CODEGEN_MODEL_NAME,
    generate_script,
)
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.routes.hosts import _require_hosts
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import JobStatus
from server.runner import DONE_SENTINEL

log = logging.getLogger(__name__)

from server.hub.routes.sessions._base import *  # noqa: F401,F403

@router.post("/sessions/{session_id}/ask")
async def session_ask(session_id: str, body: dict) -> dict:
    """LLM に yes/no 質問を投げて bool を返す。

    body:
      ``{"question": "...", "engine": "auto"}``

    response: ``{"status": "OK", "result": true|false, "elapsed_ms": int}``

    ``engine`` で AI Engines 管理画面に登録した chat 系エンジンの
    slug を指定 (例: ``"chatgpt51"``, ``"qwen-chat"``, ``"claude"``)。
    省略 / ``"auto"`` は promoted な chat エンジンを採用 (operator
    が AI Engines タブで指定したデフォルト)。worker は hub の
    ``/engines/.../resolve`` を叩いて endpoint + model + API key を
    解決してから LLM を呼ぶので、worker のローカル env に API キー
    を撒く必要は無い。

    Worker 側で現在の outline + URL を prompt に入れて LLM に渡し、
    厳密な "yes" / "no" 1 ワード回答を引き出す。パース不能なら False
    (= 無作為に True に倒れない安全側) に倒す。Macro UI の
    ``If (Agent)`` 行と ``page.ask()`` SDK メソッドが裏で叩く。
    """
    body = body or {}
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "missing 'question'")
    engine = (body.get("engine") or "auto").lower()
    action = _route_to_page(
        {"kind": "ask", "question": question, "engine": engine},
        body,
    )
    out = await _send_session_action(session_id, action, timeout=45.0)
    return {
        "status": out.get("status", "OK"),
        "result": bool(out.get("result")),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/extract")
async def session_extract(session_id: str, body: dict) -> dict:
    """LLM-driven structured extraction (paprika-native).

    The SDK builds a JSON Schema from a Pydantic model and posts it
    here along with the natural-language instruction. The worker
    feeds the current page outline + the JSON Schema + the instruction
    to a chat engine, parses the JSON response, and returns it for
    SDK-side validation.

    body::

        {
          "instruction":  "<what to extract>",
          "schema_json":  "<JSON Schema string built by the SDK>",
          "engine":       "auto" | "<engine slug>",
          "context":      "outline" | "html",
          "max_chars":    12000,
          "variables":    {"name": "<value>", ...}  # optional
        }

    response::

        {"status": "OK", "result": <parsed JSON>, "elapsed_ms": int}

    The Pydantic validation step happens on the SDK side so the
    user's full type hint is honoured. The hub / worker layer here
    deliberately keeps the response shape as plain JSON so any
    future client (PHP, CLI, curl) can use the same endpoint.
    """
    body = body or {}
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "missing 'instruction'")
    schema_json = (body.get("schema_json") or "").strip()
    engine = (body.get("engine") or "auto").lower()
    context = (body.get("context") or "outline").lower()
    if context not in ("outline", "html"):
        context = "outline"
    max_chars = int(body.get("max_chars") or 12000)
    action = _route_to_page(
        {
            "kind": "extract",
            "instruction": instruction,
            "schema_json": schema_json,
            "engine": engine,
            "context": context,
            "max_chars": max_chars,
            "variables": dict(body.get("variables") or {}),
        },
        body,
    )
    out = await _send_session_action(session_id, action, timeout=90.0)
    return {
        "status": out.get("status", "OK"),
        "result": out.get("result"),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/observe")
async def session_observe(session_id: str, body: dict) -> dict:
    """LLM-driven candidate enumeration (paprika-native).

    Ask the LLM to look at the outline and propose up to
    ``max_results`` elements matching the operator's intent. NOTHING
    is executed; the SDK reshapes the JSON into :class:`Candidate`
    objects that the script can inspect, then explicitly pass to
    ``page.click`` / ``page.fill``.

    body::

        {
          "intent":       "<natural-language description>",
          "engine":       "auto" | "<engine slug>",
          "max_results":  5,
          "variables":    {"name": "<value>", ...}  # optional
        }

    response::

        {
          "status": "OK",
          "result": [
            {"selector": "[data-paprika-id=\\"3\\"]",
             "description": "...", "method": "click",
             "arguments": null, "paprika_id": 3, "confidence": 0.92},
            ...
          ],
          "elapsed_ms": int
        }
    """
    body = body or {}
    intent = (body.get("intent") or "").strip()
    if not intent:
        raise HTTPException(400, "missing 'intent'")
    engine = (body.get("engine") or "auto").lower()
    max_results = int(body.get("max_results") or 5)
    action = _route_to_page(
        {
            "kind": "observe",
            "intent": intent,
            "engine": engine,
            "max_results": max_results,
            "variables": dict(body.get("variables") or {}),
        },
        body,
    )
    out = await _send_session_action(session_id, action, timeout=60.0)
    return {
        "status": out.get("status", "OK"),
        "result": out.get("result") or [],
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/agent")
async def session_agent(session_id: str, body: dict) -> dict:
    """Run a localised LLM agent loop on an open session.

    Body::

        {"goal": "Dismiss any popups", "max_steps": 3}

    Returns ``{completed, steps_taken, summary, last_action, error}``.
    Implements ``page.agent(goal, max_steps)`` in the SDK; useful for
    hybrid scripts where most logic is deterministic but a few spots
    (age gates, login dialogs, "find the play button") need LLM
    judgement.
    """
    body = body or {}
    goal = (body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "missing 'goal'")
    max_steps = int(body.get("max_steps") or 5)
    if max_steps < 1 or max_steps > 30:
        raise HTTPException(400, "max_steps must be in [1, 30]")
    engine = (body.get("engine") or "auto").lower()
    if engine not in ("auto", "qwen", "cogagent"):
        raise HTTPException(
            400,
            "engine must be 'auto', 'qwen', or 'cogagent'",
        )
    info = _get_session_or_404(session_id)
    worker = state.registry.connections.get(info.worker_id)
    if worker is None:
        raise HTTPException(
            502,
            f"session worker '{info.worker_id}' is no longer connected",
        )
    async with info.lock:
        info.state = "running"
        info.current_action = f"agent({max_steps}, engine={engine})"
        # See _send_session_action for why we refresh here too.
        info.last_active_at = datetime.utcnow()
        # Per-step timeout. cogagent step takes ~2s, qwen ~3-8s, the
        # 'auto' chain ~10s.  20s/step gives healthy headroom while
        # bailing fast when the session is wedged (Chrome hang, dead
        # worker, runaway agent loop).  Was 60s/step before -- a single
        # 3-step call would block 180s on a wedged session, burning
        # the codegen attempt's budget for no useful work.
        # Operator override: ``step_timeout_s`` in the request body.
        step_timeout_s = float(body.get("step_timeout_s") or 20.0)
        if step_timeout_s < 5.0:
            step_timeout_s = 5.0
        if step_timeout_s > 120.0:
            step_timeout_s = 120.0
        try:
            reply = await worker.session_agent(
                session_id,
                goal,
                max_steps,
                engine=engine,
                timeout=max(15.0, max_steps * step_timeout_s),
            )
        except TimeoutError:
            raise HTTPException(504, "page.agent() timed out")
        except Exception as e:
            raise HTTPException(502, f"page.agent() send failed: {e}")
        finally:
            info.current_action = None
            info.state = "idle"
    info.last_active_at = datetime.utcnow()
    return {
        "completed": reply.completed,
        "steps_taken": reply.steps_taken,
        "summary": reply.summary,
        "last_action": reply.last_action,
        "error": reply.error,
        # Per-step trace -- the SDK prints these continuation lines
        # after the [paprika] action log so the job log shows what
        # the agent actually did. Empty when the worker emitted no
        # actions or when an older worker without the field replies.
        "steps": list(getattr(reply, "steps", None) or []),
    }


@router.post("/codegen")
async def codegen(body: dict) -> dict:
    """Generate a paprika-client script from a natural-language task.

    Body::

        {
          "goal": "Open HN, click each story link in order, capture each",
          "hub_url": "http://paprika.lan",     // optional
          "extra_context": "...",                    // optional
          "max_tokens": 2000,                        // optional
          "temperature": 0.1,                         // optional
          "engine": "chatgpt51"                       // optional, default env
        }

    Returns ``{code, raw, model, elapsed_ms, finish_reason, usage,
    tool_calls}``. ``tool_calls`` lists any web_search calls the model
    made -- empty when the engine doesn't speak OpenAI tools or didn't
    need to look anything up. Server-side execution is NOT performed --
    the operator copies the code out and runs it themselves.
    """
    goal = (body or {}).get("goal") or ""
    if not goal.strip():
        raise HTTPException(400, "missing 'goal'")
    hub_url = (body or {}).get("hub_url") or "http://hub:8000"
    extra = (body or {}).get("extra_context")
    # Optional engine routing. Lets the admin UI (and curl-from-the-
    # terminal smoke tests) target a specific registered engine instead
    # of always falling through to the env-default CODEGEN_LLM_URL.
    # Unknown slug -> resolve_engine_target falls back to env defaults
    # internally (with a stderr note), so a stale slug isn't fatal.
    from server.hub.codegen import resolve_engine_target as _resolve_engine

    engine_slug = ((body or {}).get("engine") or "").strip() or None
    llm_target = _resolve_engine(engine_slug, state.engines) if engine_slug else None
    try:
        out = await generate_script(
            goal,
            hub_url=hub_url,
            extra_context=extra,
            max_tokens=int((body or {}).get("max_tokens") or 2000),
            temperature=float((body or {}).get("temperature") or 0.1),
            target=llm_target,
            download_video=bool((body or {}).get("download_video", False)),
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"LLM call failed: {e}")
    return out


@router.get("/codegen/info")
async def codegen_info() -> dict:
    """Expose which LLM the hub will use so the UI can show it."""
    return {
        "llm_url": CODEGEN_LLM_URL,
        "model_name": CODEGEN_MODEL_NAME,
    }


@router.post("/sessions/{session_id}/solve_cloudflare")
async def session_solve_cloudflare(session_id: str, body: dict) -> dict:
    """Wait out a Cloudflare 'Just a moment...' managed challenge on
    the session's current page.

    Body (all optional): ``{timeout_s: float, page_id: str}``.
    Returns ``{status, result: {cleared, title, waited_s}}``.

    nodriver is an undetected real Chrome, so the common Cloudflare
    *managed* challenge auto-passes within a few seconds of loading
    -- this just polls the page title until the challenge marker is
    gone. A challenge that demands an explicit Turnstile checkbox
    click is NOT solved here (operator clicks it via noVNC; the
    resulting cf_clearance cookie auto-saves to /hosts/{host} and is
    reused on later sessions since the worker fleet shares an egress
    IP + Chrome UA).
    """
    body = body or {}
    timeout_s = float(body.get("timeout_s") or 25.0)
    if timeout_s < 1:
        timeout_s = 1.0
    if timeout_s > 180:
        timeout_s = 180.0
    action: dict = {"kind": "solve_cloudflare", "timeout_s": timeout_s}
    if "click_checkbox" in body:
        action["click_checkbox"] = bool(body["click_checkbox"])
    action = _route_to_page(action, body)
    return await _send_session_action(
        session_id,
        action,
        # +30 covers the post-click re-poll window (~12s) + verify_cf
        # screenshot/template work on top of the wait timeout.
        timeout=timeout_s + 30.0,
    )


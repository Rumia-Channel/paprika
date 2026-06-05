"""Worker source self-update + version-hash computation + plugin fetch. (worker agent package; shared bits in _base.py)."""

from __future__ import annotations
import asyncio
import functools
import json
import os
import random
import shutil
import socket
import logging
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import httpx
from core.httpclient import make_async_client
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    fetch,
)
from server.protocol import (
    AssetInfo,
    HubAssignJob,
    HubExpectedVersion,
    HubProfileDelete,
    HubProfileSync,
    HubRegistered,
    HubPreviewSubscribe,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionInteraction,
    HubSessionStart,
    HubUpdateGate,
    JobOptions,
    JobResult,
    JobStatus,
    ProfileCacheEntry,
    SessionStateSnapshot,
    WorkerCapabilities,
    WorkerDraining,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerRegister,
    WorkerPreviewFrame,
    WorkerScreenshotReply,
    WorkerSessionActionResult,
    WorkerSessionAgentResult,
    WorkerSessionAnnounce,
    WorkerSessionEndAck,
    WorkerSessionStartAck,
    YtdlpResult,
    decode_hub_msg,
    encode_msg,
)
from server.scheduler import HEARTBEAT_INTERVAL
from server.worker import browser_ops
from server.worker.sessions import SessionState
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _evaluate_in_frame,
    _looks_like_player_iframe,
)
from server.worker.session_actions import (
    _ActionCtx,
    _SESSION_ACTIONS,
)
import re as _re

from ._base import *  # noqa: F401,F403
from ._base import VERSION_FILE, WORKER_EXIT_CODE_VERSION_MISMATCH, _CACHED_AT, _CACHED_WORKER_VERSION, _SOURCE_HASH_ROOTS, _VERSION_CACHE_TTL_S, _WORKER_PLUGIN_MAX_BYTES, _WORKER_PLUGIN_PATH_PREFIX, _WORKER_PLUGIN_ROOT, _WORKER_SOURCE_MAX_BYTES, _WORKER_SOURCE_ROOT, _WORKER_SOURCE_TARGETS, _logger

def _compute_source_version() -> str:
    """SHA-256 of every ``.py`` under /app/server and /app/core,
    truncated to 12 hex chars. Empty string if neither dir is present
    or the walk fails entirely (caller falls back to VERSION / env).

    Files are hashed in sorted, path-prefixed order so the result is
    stable across hosts as long as the tree contents match. Symlinks
    and non-files are silently skipped.
    """
    try:
        import hashlib

        h = hashlib.sha256()
        any_file = False
        for root in _SOURCE_HASH_ROOTS:
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*.py")):
                if not p.is_file():
                    continue
                rel_posix = p.relative_to("/app").as_posix()
                # Worker self-update tracks ONLY code the worker actually runs.
                # Skip hub-only modules (server/hub/** routes/UI/registry, and
                # server/scheduler.py) so editing them never churns the fleet --
                # workers would otherwise self-update for code they don't import.
                # (The worker reads only HEARTBEAT_INTERVAL from scheduler.py; it
                # changes ~never and rides the next real worker-code update.)
                # MUST stay byte-identical to the skip in
                # server/hub/_version.py:_compute_hub_source_version().
                if rel_posix.startswith("server/hub/") or rel_posix == "server/scheduler.py":
                    continue
                try:
                    rel = rel_posix.encode("utf-8")
                    h.update(rel)
                    h.update(b"\0")
                    h.update(p.read_bytes())
                    any_file = True
                except Exception:
                    continue
        if not any_file:
            return ""
        return h.hexdigest()[:12]
    except Exception:
        return ""


def default_worker_version() -> str:
    """Identify which build this worker is running.

    Resolution order:
      1. SHA-256 hash of the source tree (``server/`` + ``core/``).
         Deterministic across hosts so the handshake comparison
         actually works. Re-walked every ``_VERSION_CACHE_TTL_S`` so a
         live bind-mount edit on the hub host is visible to the still-
         running hub process within seconds (without this, the hub
         keeps reporting its boot-time hash and any worker that picks
         up the new source via rsync+restart looks "newer" than hub,
         triggering the self-update loop -- see the 2026-05-25 post-
         mortem in CHANGELOG).
      2. ``/app/VERSION`` file (legacy; ``scripts/sync-workers.sh``
         used to write a ``${SHA} ${TS}`` stamp here -- still honored).
      3. ``WORKER_VERSION`` env var override.
      4. ``"dev"`` sentinel (kept for the case where none of the
         above can produce a string; treated as "I don't know my
         version" by ``_versions_meaningfully_differ``).

    Cached with a TTL so a bind-mount source change shows up at the
    next handshake instead of requiring a process restart. Fallback
    paths (2-4) only matter when source-hash returns empty, and they
    don't change at runtime, so they keep the historical permanent-
    cache behaviour.
    """
    global _CACHED_WORKER_VERSION, _CACHED_AT
    now = time.monotonic()
    if (
        _CACHED_WORKER_VERSION is not None
        and (now - _CACHED_AT) < _VERSION_CACHE_TTL_S
    ):
        return _CACHED_WORKER_VERSION

    # Try source-hash first. This is the only path whose result can
    # change while the process is alive; refreshing it is the whole
    # point of having a TTL here.
    v = _compute_source_version()
    if v:
        _CACHED_WORKER_VERSION = v
        _CACHED_AT = now
        return v

    # Fallbacks: source tree unavailable (test contexts, missing
    # bind-mount, etc). Once these resolve we keep the answer forever
    # -- they don't change at runtime.
    if _CACHED_WORKER_VERSION is not None:
        # Already resolved via a fallback on a prior call; just refresh
        # the timestamp so we don't thrash the work in case the fall-
        # back paths are themselves I/O-heavy.
        _CACHED_AT = now
        return _CACHED_WORKER_VERSION
    try:
        if VERSION_FILE.exists():
            disk = VERSION_FILE.read_text().strip()
            if disk:
                _CACHED_WORKER_VERSION = disk
                _CACHED_AT = now
                return disk
    except Exception:
        pass
    env = os.environ.get("WORKER_VERSION", "").strip()
    _CACHED_WORKER_VERSION = env or "dev"
    _CACHED_AT = now
    return _CACHED_WORKER_VERSION


def _auto_exit_on_version_mismatch() -> bool:
    """Whether to ``sys.exit(42)`` on a detected version mismatch.

    On by default -- the user explicitly opted into the "warn + auto-exit"
    behavior so the docker restart policy can pull the new image. Set
    ``PAPRIKA_WORKER_AUTO_EXIT_ON_VERSION_MISMATCH=0`` to downgrade to
    warning-only (the worker keeps running but logs a banner on every
    successful registration).
    """
    val = (
        os.environ.get(
            "PAPRIKA_WORKER_AUTO_EXIT_ON_VERSION_MISMATCH",
            "1",
        )
        .strip()
        .lower()
    )
    return val in ("1", "true", "yes", "on")


def _versions_meaningfully_differ(local: str, expected: str) -> bool:
    """Decide whether ``local`` vs ``expected`` should trigger a warning.

    Both must be non-empty. The ``"dev"`` sentinel means "this side
    can't compute a real version" (e.g. source tree absent, hash
    walk failed) -- if BOTH sides report dev, neither knows anything
    actionable; otherwise the side that DOES know wins and a mismatch
    fires normally. Previously *either* dev was a blanket no-op, which
    silently disabled auto-update whenever the (literal ``"dev"``)
    VERSION file was left untouched.
    """
    if not local or not expected:
        return False
    if local == "dev" and expected == "dev":
        return False
    return local != expected


def _print_version_mismatch_banner(
    *,
    local: str,
    expected: str,
    source: str,
) -> None:
    """Emit a hard-to-miss banner so operators notice in busy log output."""
    bar = "!" * 60
    lines = [
        "",
        bar,
        "!! PAPRIKA WORKER VERSION MISMATCH",
        f"!!   reported by:  {source}",
        f"!!   expected:     {expected}",
        f"!!   this worker:  {local}",
        "!!",
        "!! To upgrade:",
        "!!   docker compose pull worker && docker compose up -d worker",
        "!!",
        "!! Disable auto-exit with:",
        "!!   PAPRIKA_WORKER_AUTO_EXIT_ON_VERSION_MISMATCH=0",
        bar,
        "",
    ]
    _logger.info("\n".join(lines))


async def _check_github_release_once(*, log_prefix: str = "[worker]") -> None:
    """Optional GitHub-releases version check, run once at worker startup.

    Disabled unless ``PAPRIKA_GITHUB_REPO=owner/repo`` is set. Useful as
    a fallback when the worker can reach the public internet but cannot
    reach the hub (e.g. a hub-misconfiguration window where you'd rather
    have outdated workers self-restart than silently keep running).

    Best-effort: network failures, rate limits, malformed responses and
    private-repo 404s are all logged at info level and swallowed -- a
    flaky GitHub should not gate the worker from starting up.
    """
    repo = os.environ.get("PAPRIKA_GITHUB_REPO", "").strip()
    if not repo:
        return
    local = default_worker_version()
    if local == "dev":
        # Dev builds (bind-mounted source) intentionally have no
        # comparable version, so skip the noise.
        return

    headers = {
        "User-Agent": "paprika-worker",
        "Accept": "application/vnd.github+json",
    }
    tok = os.environ.get("PAPRIKA_GITHUB_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        async with make_async_client(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        _logger.info(
            f"{log_prefix} GitHub version check skipped ({type(e).__name__}: {e})",
        )
        return

    tag = (data.get("tag_name") or "").strip()
    # Allow either "v1.2.3" or "1.2.3" in the release tag; the VERSION
    # file usually omits the leading 'v'.
    expected = tag[1:] if tag.startswith(("v", "V")) else tag

    if _versions_meaningfully_differ(local=local, expected=expected):
        _print_version_mismatch_banner(
            local=local,
            expected=expected,
            source=f"GitHub releases ({repo})",
        )
        if _auto_exit_on_version_mismatch():
            _logger.info(
                f"{log_prefix} exiting with code "
                f"{WORKER_EXIT_CODE_VERSION_MISMATCH} so the supervisor "
                f"can pull the new image",
            )
            sys.exit(WORKER_EXIT_CODE_VERSION_MISMATCH)


def _auto_fetch_source() -> bool:
    """Whether to download + apply the hub's source tarball on version
    mismatch. On by default; set
    ``PAPRIKA_WORKER_AUTO_FETCH_SOURCE=0`` to fall back to the previous
    "log banner + exit(42)" behaviour (useful when the worker's source
    is git-tracked and you don't want auto-overwrites)."""
    val = (
        os.environ.get(
            "PAPRIKA_WORKER_AUTO_FETCH_SOURCE",
            "1",
        )
        .strip()
        .lower()
    )
    return val in ("1", "true", "yes", "on")


def _validate_tar_member(name: str) -> str:
    """Reject obviously hostile tar entry paths.

    Returns the cleaned name (forward-slash, no leading slash) on
    success, raises ValueError otherwise. The accepted shape::

        server/...   |   core/...   |   VERSION
    """
    if not name:
        raise ValueError("empty member name")
    if name.startswith("/"):
        raise ValueError(f"absolute path in tarball: {name!r}")
    parts = name.replace("\\", "/").split("/")
    if any(p in ("", "..") for p in parts):
        raise ValueError(f"path traversal in tarball: {name!r}")
    top = parts[0]
    if top not in _WORKER_SOURCE_TARGETS:
        raise ValueError(f"unexpected top-level in tarball: {top!r}")
    return "/".join(parts)


async def _fetch_and_apply_source_from_hub(
    *,
    hub_http_url: str,
    log_prefix: str = "[worker]",
) -> bool:
    """Download the hub's source tarball and apply it over /app/ in-place.

    Designed to be called immediately before ``sys.exit(42)`` when the
    handshake reports a version mismatch -- the process is about to die
    anyway, so we can overwrite the bind-mounted paths without
    worrying about file-in-use semantics. The docker restart policy
    then boots a fresh process which loads the new code from those
    bind mounts.

    **In-place update, not directory swap.** ``/app/server`` etc. are
    typically docker bind mounts -- you can't ``rename()`` a mountpoint
    (EBUSY / "device or resource busy"). So we walk each tarball entry
    and write it directly to its target path; directories are created
    on demand. After extraction we walk the live tree and delete files
    that weren't in the tarball, so a file removed upstream actually
    disappears locally (avoids stale-module imports after upgrades
    that rename or delete things).

    Safety / validation:
      * Cap tarball size at 50 MB to avoid memory blow-up.
      * Reject absolute paths and ``..`` segments (path traversal).
      * Reject top-level entries outside the agreed whitelist
        (server / core / VERSION).
      * Per-file atomic write: temp file in the same dir + rename.
      * Failure is non-fatal: caller is expected to ``sys.exit(42)``
        regardless, so a botched download just means the next boot
        runs on the same stale code (and the version-mismatch banner
        keeps firing until someone investigates).

    Returns True on a clean, successfully-applied update; False on any
    failure.
    """
    import io
    import os as _os
    import shutil
    import tarfile

    url = f"{hub_http_url.rstrip('/')}/worker-source.tar.gz"
    try:
        async with make_async_client(timeout=120.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            body = r.content
    except Exception as e:
        _logger.info(
            f"{log_prefix} self-update: tarball fetch failed "
            f"({type(e).__name__}: {e}); will exit anyway",
        )
        return False

    if len(body) > _WORKER_SOURCE_MAX_BYTES:
        _logger.info(
            f"{log_prefix} self-update: tarball too large "
            f"({len(body)} bytes > {_WORKER_SOURCE_MAX_BYTES}); aborting",
        )
        return False

    # Validate + collect entries. We DON'T extract to a staging dir
    # because that would require renaming directories into place at the
    # end, and our targets are bind mounts (un-renameable).
    paths_seen: set[Path] = set()  # absolute paths that we wrote to
    dirs_touched: set[str] = set()  # top-level prefixes we touched
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                _validate_tar_member(m.name)
            for m in members:
                target = _WORKER_SOURCE_ROOT / m.name
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not (m.isfile() or m.isreg()):
                    # Skip symlinks / devices / hardlinks; we never
                    # publish those in the hub tarball.
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # Atomic per-file write: extract into a sibling temp
                # file, then rename. fsync optional; bind-mount targets
                # are non-critical durability-wise.
                fobj = tar.extractfile(m)
                if fobj is None:
                    continue
                tmp = target.with_name(target.name + ".paprika-tmp")
                try:
                    with open(tmp, "wb") as out:
                        shutil.copyfileobj(fobj, out)
                    try:
                        _os.replace(tmp, target)
                    except OSError as rename_err:
                        # EBUSY (16) on Linux when the target is itself
                        # a docker bind mount -- typically a single-file
                        # mount like /app/VERSION. The kernel refuses
                        # renames onto a mountpoint, so fall back to
                        # writing the bytes directly into the live file.
                        # Loses per-file atomicity, which is fine here
                        # because the process is about to exit(42) anyway.
                        if getattr(rename_err, "errno", None) == 16:
                            with open(tmp, "rb") as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                        else:
                            raise
                finally:
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except Exception:
                            pass
                paths_seen.add(target.resolve())
                dirs_touched.add(m.name.split("/", 1)[0])
    except Exception as e:
        _logger.info(
            f"{log_prefix} self-update: tarball extract failed ({type(e).__name__}: {e}); aborting",
        )
        return False

    # Prune files that exist locally under the updated trees but were
    # NOT in the tarball -- those were renamed or deleted upstream.
    # Restrict the walk to the directories we actually touched so a
    # botched / partial tarball can't wipe arbitrary places. Skip
    # VERSION since it's a single file (already overwritten above).
    pruned = 0
    for top in dirs_touched:
        if top == "VERSION":
            continue
        root = _WORKER_SOURCE_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.resolve() not in paths_seen:
                    # Don't delete our own temp-file leftovers either
                    # (extension belt-and-suspenders).
                    if path.name.endswith(".paprika-tmp"):
                        continue
                    path.unlink()
                    pruned += 1
            except Exception:
                pass

    _logger.info(
        f"{log_prefix} self-update: applied "
        f"{len(paths_seen)} file(s) across {sorted(dirs_touched)}; "
        f"pruned {pruned} stale file(s)",
    )
    return True


async def _fetch_worker_plugins_from_hub(
    *,
    hub_http_url: str,
    log_prefix: str = "[worker]",
) -> bool:
    """Best-effort sync of the hub's plugin tree into /app/data/tools/.

    Called right after a successful registration handshake so the worker
    has a current copy of every installed plugin before the main job
    loop starts. Plugins live OUTSIDE the source tarball path on
    purpose (see ``_WORKER_SOURCE_TARGETS`` comment) so a newly-bundled
    plugin can never trigger the exit-42 / refuse loop that hit the
    fleet on 2026-05-27.

    Failure is non-fatal: a worker without the latest plugins just falls
    back to whatever it has on disk (or fails the next plugin-using job
    cleanly with PluginNotAvailable). The main code path keeps running.

    Returns True on a successful extract, False otherwise.
    """
    import io
    import os as _os
    import shutil
    import tarfile

    url = f"{hub_http_url.rstrip('/')}/worker-plugins.tar.gz"
    try:
        async with make_async_client(timeout=120.0) as client:
            r = await client.get(url)
            if r.status_code == 404:
                # Hub doesn't advertise plugins yet (older hub) -- silently OK.
                return False
            r.raise_for_status()
            body = r.content
    except Exception as e:
        _logger.info(
            f"{log_prefix} plugin sync: skipped ({type(e).__name__}: {e})",
        )
        return False

    if len(body) > _WORKER_PLUGIN_MAX_BYTES:
        _logger.info(
            f"{log_prefix} plugin sync: tarball too large "
            f"({len(body)} > {_WORKER_PLUGIN_MAX_BYTES}); skipping",
        )
        return False

    target_root = _WORKER_PLUGIN_ROOT
    paths_seen: set[Path] = set()
    extracted = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                name = (member.name or "").replace("\\", "/")
                # Reject absolute paths / traversal / wrong prefix.
                if name.startswith("/") or ".." in name.split("/"):
                    _logger.info(
                        f"{log_prefix} plugin sync: rejecting unsafe path {name!r}",
                    )
                    return False
                if not name.startswith(_WORKER_PLUGIN_PATH_PREFIX):
                    # Only the data/tools/ subtree is accepted here.
                    _logger.info(
                        f"{log_prefix} plugin sync: rejecting out-of-tree path {name!r}",
                    )
                    return False
                target_path = (target_root / name).resolve()
                root_resolved = target_root.resolve()
                if root_resolved not in target_path.parents and target_path != root_resolved:
                    _logger.info(
                        f"{log_prefix} plugin sync: refusing escape via symlink {name!r}",
                    )
                    return False
                target_path.parent.mkdir(parents=True, exist_ok=True)
                fobj = tf.extractfile(member)
                if fobj is None:
                    continue
                content = fobj.read()
                tmp = target_path.with_suffix(target_path.suffix + ".tmp")
                tmp.write_bytes(content)
                _os.replace(tmp, target_path)
                # Preserve executable bit (adapters may shell out via subprocess).
                if member.mode & 0o111:
                    try:
                        _os.chmod(target_path, 0o755)
                    except Exception:
                        pass
                paths_seen.add(target_path)
                extracted += 1
    except Exception as e:
        _logger.info(
            f"{log_prefix} plugin sync: tarball extract failed "
            f"({type(e).__name__}: {e}); aborting",
        )
        return False

    # Prune local plugin files that the hub no longer ships. Limits the
    # walk to data/tools/installed/ so a stray data/tools/catalog.json
    # disappearing doesn't wipe an unrelated subtree.
    pruned = 0
    installed_root = target_root / "data" / "tools" / "installed"
    if installed_root.is_dir():
        try:
            for p in installed_root.rglob("*"):
                if p.is_file() and p not in paths_seen:
                    try:
                        p.unlink()
                        pruned += 1
                    except Exception:
                        pass
            # Sweep up empty plugin dirs left behind.
            for d in sorted(
                (p for p in installed_root.rglob("*") if p.is_dir()),
                key=lambda x: len(x.parts),
                reverse=True,
            ):
                try:
                    d.rmdir()
                except OSError:
                    pass
        except Exception:
            pass

    _logger.info(
        f"{log_prefix} plugin sync: applied {extracted} file(s); "
        f"pruned {pruned} stale file(s)",
    )
    return extracted > 0


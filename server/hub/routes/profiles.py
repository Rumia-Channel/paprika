"""Profile registry routes: /profiles/* (list, CRUD, default, install).

Operators upload Chrome profile snapshots (cookies / login state /
extensions). Jobs opt in with ``options.use_profile = "<name>"`` and
the hub instructs the worker to lay the profile's tarball into the
lane's user-data-dir before Chrome starts.

This module owns the entire profile feature surface:

* CRUD routes (GET / PUT / POST / DELETE under /profiles/)
* Default-profile management (POST/DELETE /profiles/default,
  POST /profiles/{name}/default)
* The Paprika Bridge extension install page +
  ``cookie-pusher.zip`` / ``paprika-bridge.zip`` artefacts that the
  install page serves
* All the archive helpers (_archive_to_targz / _detect_profile_remap
  / _format_bytes) used by upload_profile
* The broadcast helpers (_broadcast_profile_sync,
  _broadcast_profile_delete, _sync_all_profiles_to_worker,
  _profile_url_for_worker) -- the first two are called by the routes;
  _sync_all_profiles_to_worker is also called from worker-connect code
  still in app.py (re-exported via ``from server.hub.routes.profiles
  import _sync_all_profiles_to_worker``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from server.hub._state import config, state
from server.hub.profiles import (
    ProfileRegistry,
)
from server.hub.profiles import (
    is_valid_name as _profile_name_valid,
)
from server.protocol import HubProfileDelete, HubProfileSync

log = logging.getLogger(__name__)
router = APIRouter(tags=["Profiles"])


# Per-profile upload size cap. Operators uploading a real Chrome
# user-data-dir routinely hit a few hundred MB once a few sites with
# heavy IndexedDB are involved (twitter / discord). 500 MB default,
# overridable via PAPRIKA_PROFILE_MAX_BYTES if the operator needs more.
_PROFILE_MAX_BYTES = int(os.environ.get("PAPRIKA_PROFILE_MAX_BYTES") or 500 * 1024 * 1024)


# HTML for the extension install page. Plain string so we don't pull a
# template engine just for one page. Hub's "look" (system font stack +
# muted typography) is intentionally lighter than the admin UI -- this
# is a one-page handoff, not a tab.
_PROFILE_EXTENSION_INSTALL_HTML = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8" />
<title>Paprika Bridge -- Install</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     max-width:720px;margin:40px auto;padding:0 20px;color:#222;line-height:1.6;}
h1{font-size:24px;margin-bottom:0;}
.sub{color:#666;margin-top:4px;}
code,pre{background:#f5f5fa;padding:2px 6px;border-radius:4px;font-size:13px;}
pre{padding:12px;overflow-x:auto;line-height:1.4;}
ol li{margin:8px 0;}
.dl{display:inline-block;background:#fef5e7;border:1px solid #d4a13d;
    color:#8a5a00;padding:8px 16px;border-radius:6px;font-weight:600;
    text-decoration:none;margin:12px 0;}
.dl:hover{background:#fce5b2;}
.note{background:#fff8e1;border-left:4px solid #d4a13d;padding:10px 14px;
      margin:16px 0;font-size:14px;border-radius:0 6px 6px 0;}
</style>
</head><body>
<h1>Paprika Bridge</h1>
<p class="sub">
  Chrome と Paprika Hub をつなぐ拡張機能です。 現バージョンは「今ログインしているサイトのクッキーを
  ワンクリックで <code>/hosts/&lt;host&gt;</code> レジストリへ送る」機能を提供。
  以後ジョブで <code>options.cookies_from=&quot;example.com&quot;</code> を指定すれば
  そのログイン状態でクロールできます。 今後のバージョンで URL 転送、クリップボード共有、
  ジョブ状態取得などを追加予定。
</p>

<h2>1. ダウンロード</h2>
<a class="dl" href="/profiles/extension/paprika-bridge.zip">
  paprika-bridge.zip をダウンロード
</a>

<h2>2. Chrome に読み込ませる (Load unpacked)</h2>
<ol>
  <li>ダウンロードした <code>paprika-bridge.zip</code> を展開
       (Windows なら右クリック → すべて展開)。
       <code>paprika-bridge/</code> というフォルダができます。</li>
  <li>Chrome のアドレスバーに <code>chrome://extensions</code> を貼り付けて開く。</li>
  <li>右上の「<strong>デベロッパー モード</strong>」を ON。</li>
  <li>左上の「<strong>パッケージ化されていない拡張機能を読み込む</strong>」を押し、
       手順 1 で展開した <code>paprika-bridge/</code> フォルダを選ぶ。</li>
  <li>ツールバーに paprika ロゴが出れば成功。
       ピン留めしておくと押しやすいです。</li>
</ol>

<!--CRX_SECTION-->

<h2>3. 使い方</h2>
<ol>
  <li>ログインしておきたいサイトを Chrome で開いてログインしておく
       (普段使いの Chrome そのままで OK)。</li>
  <li>ツールバーの paprika アイコンをクリック → <code>⚙</code> から Hub URL を入力
       (例: <code>http://paprika.lan</code>)。次回からは保存される。</li>
  <li><strong>自動モード (既定):</strong> 見ているサイトにログインすると、その
       cookie が数秒後に自動で hub の <code>/hosts/&lt;host&gt;</code> に登録され、
       worker がそのまま使える。手動で送るなら「このサイトを今すぐ登録」。</li>
  <li><strong>開発モード:</strong> <code>⚙</code> で ON にすると自動登録を止め、
       cookie を<strong>表形式</strong>で可視化・編集してから登録できる。</li>
  <li>送ったホストは admin UI の <a href="/#hosts">Hosts</a> タブに出る。
       <code>cookies_from</code> で参照可能。</li>
</ol>

<div class="note">
  <strong>制限事項:</strong>
  cookie 転送のみ動作します (Chrome 拡張 API の制約)。
  Login Data SQLite / IndexedDB / Local Storage は含まれません。
  実用上、cookie だけで 90% のログインサイトには再ログイン不要で入れます。
  完全な profile (autofill / passwords / Local Storage 含む) を持ち込みたい
  場合は <code>paprika-client upload-profile</code> CLI を使ってください。
</div>

<h2>関連リンク</h2>
<ul>
  <li><a href="/#profiles">Profiles タブに戻る</a></li>
  <li><a href="https://github.com/paps-jp/paprika">paprika ソース (GitHub)</a></li>
</ul>
</body></html>
"""


# Injected into the install page (replacing the <!--CRX_SECTION--> marker)
# when a packed paprika-bridge.crx is present. Kept separate from the main
# template so it can be .format()'d without tripping over the CSS braces.
_CRX_INSTALL_FRAGMENT = """
<h2>署名付き .crx でインストール (組織展開 / デベロッパーモード不要)</h2>
<p>安定した拡張 ID を持つ署名済み .crx です。複数 PC へ Chrome ポリシーで
   強制インストールでき、デベロッパーモードの警告も出ません。</p>
<a class="dl" href="/profiles/extension/paprika-bridge.crx">paprika-bridge.crx をダウンロード</a>
<p>拡張 ID: <code>{ext_id}</code></p>
<p>Chrome ポリシー <code>ExtensionInstallForcelist</code> に次の1行を追加:</p>
<pre>{ext_id};{base}/profiles/extension/paprika-bridge/updates.xml</pre>
<p class="sub">※ 単体の .crx はドラッグ&amp;ドロップでは入りません (Chrome の仕様)。
   個人利用なら上の Load unpacked が簡単。組織配布は上記ポリシーを使います。</p>
"""


def _require_profiles() -> ProfileRegistry:
    if state.profiles is None:
        raise HTTPException(503, "profile registry not initialised")
    return state.profiles


def _profile_visible_to(meta: dict | None, request) -> bool:
    """Phase 2b read-scope: whether the caller may see / download this profile.

    Non-breaking — only a SCOPED caller (enforce + non-admin user) is
    restricted, to profiles they own or that are ``shared``. Workers
    (``kind=worker``) and admin / system are never scoped, so a worker fetching
    an assigned profile's tarball from ``GET /profiles/{name}`` always passes
    (the dispatch already decided ownership). ``meta is None`` returns True so
    the normal 404 path handles a genuinely absent profile."""
    from server.hub.auth import owner_of, should_scope
    p = getattr(getattr(request, "state", None), "principal", None)
    if not should_scope(p):
        return True
    if meta is None:
        return True
    if bool(meta.get("shared", True)):
        return True
    return str(meta.get("owner_id") or "default") == owner_of(request)


def _profile_url_for_worker(worker, name: str) -> str | None:
    """Build the GET URL a worker should use to fetch the tarball.

    Same logic as the per-job profile_url assembly: prefer the URL
    the worker dialled in on (worker.public_base_url), fall back to
    PUBLIC_BASE_URL. Returns None if neither is known -- in that
    case the worker has no way to reach back, so we skip the sync.
    """
    base = worker.public_base_url or config.public_base_url
    if not base:
        return None
    return f"{base.rstrip('/')}/profiles/{name}"


async def _broadcast_profile_sync(name: str) -> None:
    """Tell every connected worker to (re)prefetch ``name`` into its
    cache. Called after POST /profiles/{name} succeeds. Best-effort:
    a worker that's offline now will get the sync when it next
    connects via the handshake re-sync path.

    The ``is_default`` flag is filled in from the current default-
    profile state so workers know whether to install this one as
    the ambient (= applied to all idle lanes' user-data-dir so
    noVNC viewers see the operator's logged-in Chrome immediately).
    """
    if state.registry is None:
        return
    meta = await _shared_meta(name)
    if meta is None or not meta.get("etag"):
        return
    # Phase 2b: only a SHARED profile may be installed as the ambient default
    # on every idle lane (noVNC viewers would otherwise see a private tenant's
    # logged-in Chrome). A private default can still back its owner's explicit
    # use_profile jobs; it just isn't broadcast as ambient.
    is_default = (await _shared_default()) == name and bool(meta.get("shared", True))
    for w in list(state.registry.connections.values()):
        url = _profile_url_for_worker(w, name)
        if not url:
            continue
        try:
            await w.send(
                HubProfileSync(
                    name=name,
                    url=url,
                    etag=meta["etag"],
                    size_bytes=int(meta.get("size_bytes") or 0),
                    is_default=is_default,
                )
            )
        except Exception:
            log.warning(
                "profile_sync %r -> %s failed",
                name,
                w.worker_id,
                exc_info=True,
            )


async def _broadcast_profile_delete(name: str) -> None:
    """Tell every connected worker to drop its cached copy of
    ``name``. Called after DELETE /profiles/{name}.
    """
    if state.registry is None:
        return
    for w in list(state.registry.connections.values()):
        try:
            await w.send(HubProfileDelete(name=name))
        except Exception:
            log.warning(
                "profile_delete %r -> %s failed",
                name,
                w.worker_id,
                exc_info=True,
            )


async def _sync_all_profiles_to_worker(worker) -> None:
    """On worker (re)connect, send a HubProfileSync for every
    currently-registered profile so it can prefetch / verify its
    cache against our authoritative state. The is_default flag is
    set on the broadcast for the active default so the worker
    installs the ambient on its idle lanes.
    """
    if state.registry is None and state.profiles is None:
        return
    default_name = await _shared_default()
    for p in await _shared_list():
        name = p.get("name")
        etag = p.get("etag")
        if not name or not etag:
            continue
        url = _profile_url_for_worker(worker, name)
        if not url:
            continue
        try:
            await worker.send(
                HubProfileSync(
                    name=name,
                    url=url,
                    etag=etag,
                    size_bytes=int(p.get("size_bytes") or 0),
                    # Phase 2b: only a SHARED profile is installed ambiently.
                    is_default=(name == default_name and bool(p.get("shared", True))),
                )
            )
        except Exception:
            log.warning(
                "initial profile_sync %r -> %s failed",
                name,
                worker.worker_id,
                exc_info=True,
            )


def _detect_profile_remap(top_entries: dict) -> tuple[str | None, str]:
    """Decide how to remap an archive's top-level layout into the
    "User Data" shape the worker expects: ``Default/`` (the
    profile) + optional ``Local State`` at the root.

    Returns ``(rename_top, wrap_in)``:
      * ``(top_dir, "")`` -- the archive has a single non-Default
        top-level directory whose content looks like a Chrome
        profile (Preferences / Cookies / etc. inside). Catches
        the common "I zipped my 'Profile 10' folder" mistake.
      * ``(None, "Default")`` -- the archive is flat (Preferences
        directly at root). Wrap everything under ``Default/``.
      * ``(None, "")`` -- archive is already in the right shape
        (``Default/`` + ``Local State`` at root).
    """
    PROFILE_MARKERS = ("Preferences", "Cookies", "History", "Bookmarks")
    USER_DATA_MARKERS = ("Local State",)
    file_names = {n for n, k in top_entries.items() if k == "file"}
    dir_names = {n for n, k in top_entries.items() if k == "dir"}
    # Already correct shape.
    if "Default" in dir_names and any(m in file_names for m in USER_DATA_MARKERS):
        return None, ""
    # Flat profile: Preferences sitting at root -> wrap.
    if any(m in file_names for m in PROFILE_MARKERS):
        return None, "Default"
    # Single named profile dir: rename top to Default.
    if len(dir_names) == 1 and not file_names:
        only = next(iter(dir_names))
        return only, ""
    return None, ""


async def _archive_to_targz(
    src_path: Path,
    *,
    format: str,
    max_bytes: int,
) -> Path:
    """Normalise an uploaded archive (gzip-tar or ZIP) into the
    canonical User Data tar.gz the worker expects.

    Walks the entries once to detect the operator's intended Chrome
    profile layout (single Profile X dir / flat / standard) via
    :func:`_detect_profile_remap`, then re-packs as a fresh tar.gz
    with the remap applied so the worker always sees::

        Default/Preferences
        Default/Extensions/<id>/<version>/...
        Local State                          (when present in source)

    The original ``src_path`` is removed on success.

    Defences:
      * Path-escape entries (Zip Slip / tarbomb) -> 400.
      * Uncompressed total > ``max_bytes * 4`` -> 413 (bomb).
    """
    import io
    import tarfile
    import tempfile
    import zipfile

    # ---- enumerate entries from the source archive ------------------
    if format == "zip":
        z = zipfile.ZipFile(src_path, "r")
        entries: list[tuple[str, int, callable]] = []
        for info in z.infolist():
            name = info.filename
            if name.startswith("/") or ".." in name.split("/") or "\x00" in name:
                z.close()
                raise HTTPException(
                    400,
                    f"archive entry refused (escaping path): {name!r}",
                )
            if info.is_dir():
                entries.append((name, -1, None))
            else:
                entries.append(
                    (
                        name,
                        info.file_size,
                        (lambda i=info: z.read(i)),
                    )
                )
        close_src = z.close
    else:
        t = tarfile.open(src_path, "r:gz")
        entries = []
        for m in t.getmembers():
            name = m.name
            if name.startswith("/") or ".." in name.split("/") or "\x00" in name:
                t.close()
                raise HTTPException(
                    400,
                    f"archive entry refused (escaping path): {name!r}",
                )
            if m.isdir():
                entries.append((name.rstrip("/") + "/", -1, None))
            elif m.isfile():
                entries.append(
                    (
                        name,
                        m.size,
                        (lambda mm=m: t.extractfile(mm).read()),
                    )
                )
        close_src = t.close

    # ---- detect layout -----------------------------------------------
    top: dict[str, str] = {}
    for name, sz, _ in entries:
        first = name.split("/", 1)[0]
        rest = name[len(first) + 1 :].rstrip("/")
        if rest or name.endswith("/"):
            top.setdefault(first, "dir")
        else:
            top.setdefault(first, "file" if sz >= 0 else "dir")
    rename_top, wrap_in = _detect_profile_remap(top)

    def remap(name: str) -> str:
        if rename_top is not None:
            prefix = rename_top + "/"
            if name == rename_top or name == rename_top + "/":
                return "Default/"
            if name.startswith(prefix):
                return "Default/" + name[len(prefix) :]
            return name
        if wrap_in:
            ROOT_FILES = {"Local State", "First Run"}
            if name in ROOT_FILES:
                return name
            return wrap_in + "/" + name
        return name

    # ---- write normalised tar.gz -------------------------------------
    out_path = Path(
        tempfile.mkstemp(
            prefix="profile_repack_",
            suffix=".tar.gz.tmp",
        )[1]
    )
    uncompressed_total = 0
    try:
        with tarfile.open(out_path, "w:gz", compresslevel=6) as tf:
            seen_dirs: set[str] = set()
            for name, size, reader in entries:
                new_name = remap(name)
                if reader is None:
                    if not new_name.endswith("/"):
                        new_name += "/"
                    if new_name in seen_dirs:
                        continue
                    seen_dirs.add(new_name)
                    ti = tarfile.TarInfo(name=new_name)
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    tf.addfile(ti)
                    continue
                data = reader()
                uncompressed_total += len(data)
                if uncompressed_total > max_bytes * 4:
                    raise HTTPException(
                        413,
                        f"archive transcode aborted: uncompressed "
                        f"size exceeded {max_bytes * 4} bytes "
                        f"(bomb suspected).",
                    )
                ti = tarfile.TarInfo(name=new_name)
                ti.size = len(data)
                ti.mode = 0o644
                ti.mtime = 0
                tf.addfile(ti, io.BytesIO(data))
        action = "kept layout"
        if rename_top is not None:
            action = f"remapped top {rename_top!r} -> 'Default'"
        elif wrap_in:
            action = f"wrapped flat layout in {wrap_in!r}"
        log.info(
            "normalised %s upload -> tar.gz: %d bytes uncompressed -> "
            "%d bytes compressed (%s)",
            format,
            uncompressed_total,
            out_path.stat().st_size,
            action,
        )
    except HTTPException:
        try:
            out_path.unlink()
        except OSError:
            pass
        try:
            close_src()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            out_path.unlink()
        except OSError:
            pass
        try:
            close_src()
        except Exception:
            pass
        raise HTTPException(
            400,
            f"archive transcode failed: {type(e).__name__}: {e}",
        )
    finally:
        try:
            close_src()
        except Exception:
            pass
        try:
            src_path.unlink()
        except OSError:
            pass
    return out_path


# Legacy name kept for any path that still calls it; new code
# should use _archive_to_targz with format= directly.
async def _zip_to_targz(zip_path: Path, *, max_bytes: int) -> Path:
    return await _archive_to_targz(
        zip_path,
        format="zip",
        max_bytes=max_bytes,
    )


def _profile_meta_to_dict(meta, *, default_name: str | None = None) -> dict:
    d = meta.to_json()
    # Mirror the convention used by other registries: surface the
    # human-readable size next to the byte count so the admin UI
    # doesn't have to format it.
    d["size_human"] = _format_bytes(d.get("size_bytes") or 0)
    # Flag the default profile in list responses so the UI can
    # highlight it without an extra round trip to GET /profiles/default.
    d["is_default"] = default_name is not None and meta.name == default_name
    return d


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n = n / 1024
    return f"{n} ?"


# --- shared (multi-hub) profile metadata: MariaDB-first, local fallback ------
# Phase C-2: profile bytes live in MinIO and metadata in MariaDB, so any hub can
# resolve + serve any profile. These async helpers return the shared view (or
# the local file registry when there's no MariaDB pool -- dev / single hub).


async def _shared_default() -> str | None:
    pool = state.mariadb_pool
    if pool is not None:
        try:
            from server.hub.mariadb import get_default_profile
            return await get_default_profile(pool)
        except Exception:
            log.warning("profiles: MariaDB default read failed; using local", exc_info=True)
    return state.profiles.get_default() if state.profiles else None


async def _shared_meta(name: str) -> dict | None:
    """Shared metadata dict (name, etag, size_bytes, s3_key, is_default, ...) for
    one profile, or None when no hub knows it. MariaDB-first, local fallback."""
    pool = state.mariadb_pool
    if pool is not None:
        try:
            from server.hub.mariadb import get_profile_meta_row
            d = await get_profile_meta_row(pool, name)
            if d is not None:
                return d
        except Exception:
            log.warning("profiles: MariaDB meta read failed for %r; using local", name, exc_info=True)
    reg = state.profiles
    if reg is not None:
        m = reg.get_meta(name)
        if m is not None:
            d = _profile_meta_to_dict(m, default_name=reg.get_default())
            d["etag"] = reg.etag(name) or ""
            d.setdefault("s3_key", "")
            return d
    return None


async def _shared_list() -> list[dict]:
    pool = state.mariadb_pool
    if pool is not None:
        try:
            from server.hub.mariadb import load_profiles
            rows = await load_profiles(pool)
            for d in rows:
                d["size_human"] = _format_bytes(d.get("size_bytes") or 0)
            return rows
        except Exception:
            log.warning("profiles: MariaDB list failed; using local", exc_info=True)
    reg = state.profiles
    if reg is None:
        return []
    default = reg.get_default()
    out: list[dict] = []
    for m in reg.list():
        d = _profile_meta_to_dict(m, default_name=default)
        d["etag"] = reg.etag(m.name) or ""
        out.append(d)
    return out


@router.get("/profiles")
async def list_profiles(request: Request) -> dict:
    """List every uploaded Chrome profile (metadata only).

    The tarballs themselves are at ``GET /profiles/{name}`` but those
    are typically only fetched by workers when a job opts into a
    profile via ``options.use_profile``.

    Response shape::

        {
          "default": "mydefault" | null,    // auto-applied profile name
          "profiles": [{name, size_bytes, ..., is_default, owner_id, shared}, ...]
        }

    Phase 2b: a scoped caller (enforce + non-admin user) sees only the
    profiles they own plus the shared ones; off/optional/admin see all.
    """
    profiles = await _shared_list()
    from server.hub.auth import should_scope
    p = getattr(getattr(request, "state", None), "principal", None)
    if should_scope(p):
        profiles = [d for d in profiles if _profile_visible_to(d, request)]
    return {
        "default": await _shared_default(),
        "profiles": profiles,
    }


@router.get("/profiles/default")
async def get_default_profile() -> dict:
    """Return ``{name: "<profile>" | null}`` for the default profile.

    The default is auto-applied to any /jobs or /sessions request
    that doesn't set ``options.use_profile`` explicitly. None means
    no default is set -- jobs run with the lane's stock profile.
    """
    return {"name": await _shared_default()}


@router.post("/profiles/{name}/default")
async def set_default_profile(name: str) -> dict:
    """Mark ``{name}`` as the auto-applied default profile.

    Effect: subsequent /jobs and /sessions requests without an
    explicit ``options.use_profile`` will be dispatched with this
    profile in the user-data-dir. Override per-job by setting
    ``options.use_profile`` to a different name. Clear via
    ``DELETE /profiles/default``.

    Workers receive a HubProfileSync broadcast with is_default=True
    so they install this profile as the ambient -- noVNC viewers
    see the operator's logged-in Chrome on every idle lane, not
    just on lanes that happened to run a job. The previous default
    (if any) gets a is_default=False broadcast so workers clear it.

    Only one default at a time; setting a new one replaces the
    previous default.
    """
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    reg = _require_profiles()
    prev = await _shared_default()
    if await _shared_meta(name) is None:
        raise HTTPException(404, f"profile '{name}' not found")
    pool = state.mariadb_pool
    if pool is not None:
        from server.hub.mariadb import set_default_profile as _mdb_set_default
        await _mdb_set_default(pool, name)
    else:
        try:
            reg.set_default(name)
        except ValueError as e:
            raise HTTPException(404, str(e))
    log.info("profile %r set as default", name)
    # Re-broadcast the new default first so workers install it,
    # then demote the previous default. Order matters: if we
    # cleared the old one first, the workers would briefly revert
    # to stock between the two broadcasts (visible in noVNC as a
    # "logged-out flicker"). Installing-then-clearing avoids that.
    try:
        await _broadcast_profile_sync(name)
        if prev and prev != name:
            await _broadcast_profile_sync(prev)
    except Exception:
        log.warning("default-change broadcast failed", exc_info=True)
    return {"name": name, "previous": prev}


@router.delete("/profiles/default")
async def clear_default_profile() -> dict:
    """Unset the default profile. Subsequent jobs without an
    explicit ``options.use_profile`` run with the lane's stock
    profile (no extra cookies / login state). Workers also clear
    the ambient install on their idle lanes (noVNC viewers see
    fresh Chrome again).
    """
    reg = _require_profiles()
    prev = await _shared_default()
    pool = state.mariadb_pool
    if pool is not None:
        from server.hub.mariadb import set_default_profile as _mdb_set_default
        await _mdb_set_default(pool, None)
    else:
        reg.set_default(None)
    if prev:
        log.info("default profile cleared (was %r)", prev)
        # Re-broadcast the demoted name with is_default=False so
        # workers clear it from their idle lanes.
        try:
            await _broadcast_profile_sync(prev)
        except Exception:
            log.warning("default-clear broadcast failed", exc_info=True)
    return {"name": None, "previous": prev}


@router.get("/profiles/{name}")
async def download_profile(name: str, request: Request):
    """Stream the tarball for ``{name}``. Used by workers when they
    receive a HubAssignJob whose ``profile_url`` points here.

    Returns ``application/gzip``. Content-Disposition is set so a
    curl ``--remote-name`` works for ad-hoc debugging too.

    Phase 2b: a scoped caller (enforce + non-admin user) gets a 404 for a
    profile they don't own and isn't shared — so a tenant can't pull another
    tenant's login state by guessing the name. Workers / admin / system are
    never scoped (the worker fetching an assigned tarball always passes).
    """
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    reg = _require_profiles()
    # Read-scope BEFORE serving so a hidden profile 404s the same whether or
    # not it happens to be cached on this hub.
    from server.hub.auth import should_scope
    _p = getattr(getattr(request, "state", None), "principal", None)
    if should_scope(_p) and not _profile_visible_to(await _shared_meta(name), request):
        raise HTTPException(404, f"profile '{name}' not found")
    p = reg.get_tarball_path(name)
    if p is None:
        # Another hub uploaded it: pull the tarball from MinIO into the local
        # cache once (keyed off the shared metadata), then serve from disk.
        meta = await _shared_meta(name)
        if meta is not None:
            s3_key = meta.get("s3_key") or f"profiles/{name}.tar.gz"
            try:
                from server.hub import objstore
                if objstore.enabled() and await objstore.get_object(
                    s3_key, reg.tarball_target(name)
                ):
                    p = reg.get_tarball_path(name)
            except Exception:
                log.warning("profile %r MinIO pull failed", name, exc_info=True)
    if p is None:
        raise HTTPException(404, f"profile '{name}' not found")
    from fastapi.responses import FileResponse

    return FileResponse(
        path=str(p),
        media_type="application/gzip",
        filename=f"{name}.tar.gz",
    )


@router.get("/profiles/{name}/info")
async def get_profile_info(name: str, request: Request) -> dict:
    """Metadata for ``{name}`` without downloading the tarball.

    Phase 2b: scoped callers get 404 for a profile they can't see (same rule
    as the list / download)."""
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    meta = await _shared_meta(name)
    if meta is None:
        raise HTTPException(404, f"profile '{name}' not found")
    if not _profile_visible_to(meta, request):
        raise HTTPException(404, f"profile '{name}' not found")
    return meta


@router.post("/profiles/{name}")
async def upload_profile(
    name: str,
    request: Request,
) -> dict:
    """Upload a Chrome profile tarball.

    The body is the raw gzipped tarball (Content-Type:
    ``application/gzip``). For multipart uploads (CLI convenience),
    use POST /profiles/{name}/multipart instead.

    The tarball should unpack to a single ``User Data``-shaped
    directory tree (the layout produced by
    ``core.fetcher.clone_chrome_profile``). The worker extracts it
    into a per-job scratch dir and points Chrome at it.

    Optional headers the CLI can set (informational, surfaced in the
    admin UI):

      * ``X-Paprika-Source-Machine``: hostname / OS info string
      * ``X-Paprika-Chrome-Profile``: e.g. "Default" or "Profile 1"
      * ``X-Paprika-Note``: free-text note
    """
    if not _profile_name_valid(name):
        raise HTTPException(
            400,
            "invalid profile name (allowed: A-Z a-z 0-9 . _ -, max 64 chars)",
        )
    reg = _require_profiles()
    lock = state.profiles_lock
    assert lock is not None

    # Phase 2b tenancy. Stamp the uploading tenant; a scoped (enforce,
    # non-admin) user's NEW profile is private (shared=False), everything else
    # (off / optional / admin) stays the shared 'default' tenant = ambient
    # behaviour. Ownership is sticky on re-upload (ProfileRegistry.save), but
    # block a scoped user from clobbering a name they can't even see so they
    # can't overwrite / poison another tenant's profile bytes.
    from server.hub.auth import owner_of, should_scope
    _p = getattr(getattr(request, "state", None), "principal", None)
    _scoped = should_scope(_p)
    if _scoped:
        _existing = await _shared_meta(name)
        if _existing is not None and not _profile_visible_to(_existing, request):
            raise HTTPException(404, f"profile '{name}' not found")
    _owner_id = owner_of(request)
    _shared = not _scoped  # scoped user → private; off/optional/admin → shared

    source_machine = request.headers.get("x-paprika-source-machine")
    chrome_profile = request.headers.get("x-paprika-chrome-profile")
    note = request.headers.get("x-paprika-note")

    # Stream the body to a temp file rather than read() into memory --
    # operator profiles can hit ~100 MB compressed for heavy Chrome
    # users (lots of localStorage / IndexedDB).
    import tempfile

    tmp = Path(tempfile.mkstemp(prefix=f"profile_upload_{name}_", suffix=".tar.gz.tmp")[1])
    total = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > _PROFILE_MAX_BYTES:
                    raise HTTPException(
                        413,
                        f"profile too large ({total} > "
                        f"{_PROFILE_MAX_BYTES} bytes). Raise "
                        f"PAPRIKA_PROFILE_MAX_BYTES on the hub or "
                        f"upload a slimmer profile.",
                    )
                f.write(chunk)
        # Sniff the magic bytes. We accept either:
        #   * gzip-wrapped tar (1f 8b)  -- canonical, save as-is
        #   * ZIP (PK\3\4 or PK\5\6)    -- Windows "Send to ->
        #                                  Compressed (zipped) folder"
        #                                  output. We unzip + retar+gzip
        #                                  so the on-disk format the
        #                                  worker fetches stays
        #                                  consistent regardless of
        #                                  upload origin.
        # Other formats (plain tar, JSON, garbage) get a targeted 400
        # with a hint about what the operator probably did wrong.
        with open(tmp, "rb") as f:
            magic = f.read(4)
        is_gzip = magic.startswith(b"\x1f\x8b")
        is_zip = magic.startswith(b"PK\x03\x04") or magic.startswith(b"PK\x05\x06")
        if not (is_gzip or is_zip):
            hint: str
            if magic[:5] == b"ustar" or (
                len(magic) >= 4 and magic[:4].isalnum() and (b"\x00" not in magic[:4])
            ):
                hint = (
                    "this looks like a plain tar archive (no gzip "
                    "wrapper). Use `tar czf ...` (note the `z`) "
                    "instead of `tar cf ...`, gzip the .tar first, "
                    "or just upload a .zip instead."
                )
            elif magic[:1] in (b"{", b"[", b"<"):
                hint = (
                    f"this looks like text content (starts with "
                    f"{magic[:1]!r}), not an archive. Did the "
                    "upload tool transcode to JSON / XML?"
                )
            else:
                hint = (
                    f"first bytes were {magic.hex()} -- expected 1f 8b for gzip or 50 4b for zip."
                )
            raise HTTPException(
                400,
                f"uploaded body is not a recognised archive: {hint}",
            )

        # ALWAYS normalise the archive layout to the worker's
        # expected "User Data" shape -- ZIP or tar.gz, regardless
        # of how the operator built it. Catches three common
        # mistakes in one place:
        #   1. ZIP from Windows Explorer "Send to -> Compressed
        #      (zipped) folder" (transcode + normalise)
        #   2. tar/zip of a non-Default profile dir like
        #      "Profile 10/" (rename top -> "Default")
        #   3. flat archive with Preferences at root (wrap in
        #      "Default/")
        # Already-correct uploads pass through as a no-op rename
        # but get re-packed for hash consistency.
        tmp = await _archive_to_targz(
            tmp,
            format=("zip" if is_zip else "gzip"),
            max_bytes=_PROFILE_MAX_BYTES,
        )

        async with lock:
            meta = reg.save(
                name,
                tarball_src=tmp,
                source_machine=source_machine,
                chrome_profile_name=chrome_profile,
                note=note,
                owner_id=_owner_id,
                shared=_shared,
            )
            # Phase C-2: push the tarball to MinIO + metadata to MariaDB so any
            # hub can serve this profile for a job (bytes are large -> MinIO,
            # not a DB BLOB). Best-effort; the local copy still serves this hub.
            _s3key = f"profiles/{name}.tar.gz"
            _etag = reg.etag(name) or ""
            try:
                from server.hub import objstore
                _tar = reg.get_tarball_path(name)
                if _tar is not None and objstore.enabled():
                    await objstore.put_object(_s3key, _tar)
            except Exception:
                log.warning("profile %r MinIO upload failed", name, exc_info=True)
            if state.mariadb_pool is not None:
                try:
                    from server.hub.mariadb import upsert_profile_row
                    await upsert_profile_row(state.mariadb_pool, meta, _etag, _s3key)
                except Exception:
                    log.warning("profile %r MariaDB upsert failed", name, exc_info=True)
        log.info(
            "profile %r uploaded: %d bytes (machine=%r chrome_profile=%r)",
            name,
            meta.size_bytes,
            source_machine,
            chrome_profile,
        )
        # Pre-push to every connected worker so the next job that
        # uses this profile finds it already in the local cache.
        # Fire-and-forget; the on-demand fetch path still works for
        # workers that miss the broadcast.
        try:
            await _broadcast_profile_sync(name)
        except Exception:
            log.warning(
                "profile %r broadcast failed", name, exc_info=True
            )
        return _profile_meta_to_dict(meta)
    finally:
        # save() moves the tmp file on success; on failure remove it
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@router.get("/profiles/extension/install", include_in_schema=False)
async def profile_extension_install_page(request: Request) -> HTMLResponse:
    """Landing page with the Paprika Bridge .zip + install instructions.

    Operator clicks the "Paprika Bridge" link in the admin UI's
    Profiles tab, lands here, downloads the .zip (load-unpacked) or the
    signed .crx (force-install via policy), follows the instructions.
    The .zip is built on the fly from
    server/web/extensions/paprika-bridge/; the .crx is the pre-signed
    committed artefact (see scripts/pack_paprika_bridge_crx.py).
    """
    ext_id = _paprika_bridge_ext_id()
    if ext_id and _BRIDGE_CRX.exists():
        base = str(request.base_url).rstrip("/")
        crx_section = _CRX_INSTALL_FRAGMENT.format(ext_id=ext_id, base=base)
    else:
        # No packed .crx in this build -> show the zip/load-unpacked flow only.
        crx_section = ""
    html = _PROFILE_EXTENSION_INSTALL_HTML.replace("<!--CRX_SECTION-->", crx_section)
    return HTMLResponse(html)


def _build_paprika_bridge_zip() -> Response:
    """Shared helper for both the new and the legacy zip URLs.

    The source lives in the hub bind-mount so a `git pull` is enough
    to refresh the served bundle -- no rebuild needed. If the
    directory isn't present (= older source tree), 404 so the
    install page surfaces a clear error.
    """
    import io
    import zipfile

    from fastapi.responses import Response

    src = Path(__file__).resolve().parents[2] / "web" / "extensions" / "paprika-bridge"
    if not src.exists():
        raise HTTPException(404, "extension source not bundled in this hub build")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(src)))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="paprika-bridge.zip"',
        },
    )


@router.get("/profiles/extension/paprika-bridge.zip", include_in_schema=False)
async def profile_extension_download():
    """Build a fresh .zip of the Paprika Bridge extension on every
    download. See ``_build_paprika_bridge_zip``.
    """
    return _build_paprika_bridge_zip()


# Legacy URL (0.1 was distributed as paprika-cookie-pusher.zip).
# Kept around for one release cycle so operators who have the old
# URL bookmarked don't hit a 404 mid-upgrade. Drop after 0.3.
@router.get("/profiles/extension/cookie-pusher.zip", include_in_schema=False)
async def profile_extension_download_legacy():
    return _build_paprika_bridge_zip()


# ---- signed CRX + force-install (Omaha) ---------------------------------
# Mirrors the paprika-agent.crx model: a pre-signed, COMMITTED .crx the hub
# serves statically. This gives the Bridge a STABLE extension ID and lets
# operators force-install it across machines via an enterprise policy
# (ExtensionInstallForcelist=<id>;<updates.xml-url>) -- no Developer-mode
# nag, auto-updates. We pre-sign (rather than sign on the fly) because the
# hub image ships no crypto lib and the .34 deploy-watcher does NOT rebuild
# images: a committed .crx ships as a plain server/ file, byte-identical on
# every hub, so the ID is fleet-wide consistent. Re-pack after editing the
# source with scripts/pack_paprika_bridge_crx.py. The signing key stays
# operator-held (never on the hub); the hub only serves the public .crx.

_BRIDGE_EXT_ROOT = Path(__file__).resolve().parents[2] / "web" / "extensions"
_BRIDGE_DIR = _BRIDGE_EXT_ROOT / "paprika-bridge"
_BRIDGE_CRX = _BRIDGE_EXT_ROOT / "paprika-bridge.crx"


def _paprika_bridge_ext_id() -> str | None:
    """Derive the stable extension ID from the committed manifest's ``key``
    (base64 SPKI): first 128 bits of SHA256, each nibble mapped 0..15 ->
    'a'..'p'. None when the manifest carries no ``key`` (un-signed source
    tree) -- callers then fall back to the zip/load-unpacked flow."""
    import base64
    import hashlib
    import json as _json

    try:
        man = _json.loads((_BRIDGE_DIR / "manifest.json").read_text("utf-8"))
        spki = base64.b64decode(man["key"])
    except Exception:
        return None
    digest = hashlib.sha256(spki).digest()[:16]
    return "".join(chr(97 + (b >> 4)) + chr(97 + (b & 0x0F)) for b in digest)


@router.get("/profiles/extension/paprika-bridge.crx", include_in_schema=False)
async def profile_extension_crx():
    """Serve the pre-signed Paprika Bridge CRX3 (stable extension ID).

    404 with a clear hint when the .crx hasn't been packed yet (run
    scripts/pack_paprika_bridge_crx.py and redeploy). Use the .zip +
    load-unpacked flow for dev; the .crx is for force-install via an
    enterprise policy (see the install page / updates.xml below).
    """
    if not _BRIDGE_CRX.exists():
        raise HTTPException(
            404,
            "paprika-bridge.crx is not packed in this build -- run "
            "scripts/pack_paprika_bridge_crx.py and redeploy.",
        )
    return Response(
        content=_BRIDGE_CRX.read_bytes(),
        media_type="application/x-chrome-extension",
        headers={
            "Content-Disposition": 'attachment; filename="paprika-bridge.crx"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/profiles/extension/paprika-bridge/updates.xml", include_in_schema=False)
async def profile_extension_updates(request: Request):
    """Omaha update manifest for force-installing / auto-updating the Bridge.

    Operators set the Chrome policy
    ``ExtensionInstallForcelist = <id>;<this-url>`` to push the extension
    to managed machines (silent install, auto-update, no Developer-mode
    nag). The <updatecheck> version comes from the packed manifest, so a
    re-pack with a bumped ``version`` triggers Chrome's auto-update.
    """
    import json as _json

    ext_id = _paprika_bridge_ext_id()
    if ext_id is None or not _BRIDGE_CRX.exists():
        raise HTTPException(404, "paprika-bridge.crx is not packed in this build")
    try:
        ver = _json.loads(
            (_BRIDGE_DIR / "manifest.json").read_text("utf-8")
        ).get("version", "0.0.0")
    except Exception:
        ver = "0.0.0"
    base = str(request.base_url).rstrip("/")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gupdate xmlns="http://www.google.com/update2/response" protocol="2.0">\n'
        f'  <app appid="{ext_id}">\n'
        f'    <updatecheck codebase="{base}/profiles/extension/paprika-bridge.crx" '
        f'version="{ver}" />\n'
        '  </app>\n'
        '</gupdate>\n'
    )
    return Response(content=xml, media_type="application/xml")


@router.delete("/profiles/{name}")
async def delete_profile(name: str) -> dict:
    """Remove the tarball + metadata for ``{name}``. In-flight jobs
    that already started extracting are unaffected (they hold their
    own scratch dir). Returns ``{deleted: bool}``.
    """
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    reg = _require_profiles()
    lock = state.profiles_lock
    assert lock is not None
    async with lock:
        ok = reg.remove(name)
    pool = state.mariadb_pool
    if pool is not None:
        try:
            from server.hub.mariadb import delete_profile_row
            await delete_profile_row(pool, name)
            ok = True  # removed from the shared store even if not cached locally
        except Exception:
            log.warning("profile %r MariaDB delete failed", name, exc_info=True)
    if ok:
        log.info("profile %r deleted", name)
        try:
            await _broadcast_profile_delete(name)
        except Exception:
            log.warning(
                "profile %r delete broadcast failed", name, exc_info=True
            )
    return {"deleted": ok}

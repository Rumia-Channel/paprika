"""Worker agent package (Phase 1 split from agent.py: module functions
extracted into video/selfupdate/workerid/recipe/translate/profile; the
WorkerAgent class lives in _core.py). Re-exports every top-level name so
both `from server.worker.agent import WorkerAgent/...` and bare
`import agent; agent.<fn>` keep working."""

from ._base import (  # noqa: F401
    VERSION_FILE,
    WORKER_EXIT_CODE_VERSION_MISMATCH,
    _CACHED_AT,
    _CACHED_WORKER_VERSION,
    _DLP_DEST_RE,
    _DLP_ETA_RE,
    _DLP_FF_SIZE_RE,
    _DLP_FF_SPEED_RE,
    _DLP_FF_TIME_RE,
    _DLP_PCT_RE,
    _DLP_PHLS_SEG_RE,
    _DLP_SPEED_RE,
    _JP_CHAR_RE,
    _NOVNC_PROTECTION_S,
    _SOURCE_HASH_ROOTS,
    _VERSION_CACHE_TTL_S,
    _WORKER_PLUGIN_MAX_BYTES,
    _WORKER_PLUGIN_PATH_PREFIX,
    _WORKER_PLUGIN_ROOT,
    _WORKER_SOURCE_MAX_BYTES,
    _WORKER_SOURCE_ROOT,
    _WORKER_SOURCE_TARGETS,
    _get_browser_user_agent,
    _logger,
    _session_interaction_at,
)
from .video import (  # noqa: F401
    _make_video_downloader,
    _parse_dl_progress,
    _terminate_ytdlp_descendants,
    detect_yt_dlp,
    is_session_protected,
)
from .selfupdate import (  # noqa: F401
    _auto_exit_on_version_mismatch,
    _auto_fetch_source,
    _check_github_release_once,
    _compute_source_version,
    _fetch_and_apply_source_from_hub,
    _fetch_worker_plugins_from_hub,
    _print_version_mismatch_banner,
    _validate_tar_member,
    _versions_meaningfully_differ,
    default_worker_version,
)
from .workerid import (  # noqa: F401
    WORKER_ID_FILE,
    _WorkerIdReassigned,
    _resolve_worker_id_file,
    default_worker_id,
    hub_http_base,
)
from .recipe import (  # noqa: F401
    _apply_fetch_recipe,
    _discover_player_iframes,
    _looks_suspect,
    _trigger_playback_in_frame,
)
from .translate import (  # noqa: F401
    _looks_non_english,
    _translate_to_english,
)
from .profile import (  # noqa: F401
    _normalise_extracted_profile,
    parse_attach,
)
from ._core import (  # noqa: F401
    WorkerAgent,
)

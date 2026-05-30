"""Named-preset registry for the Submit form.

A preset is a saved snapshot of "what fields go into a job submission",
plus enough metadata (mode + engine + Simple-macro row list + compiled
code) to reconstruct the form OR run the job directly via the API
without going through the UI.

Storage layout::

    {data_dir}/presets/<safe-name>.json

One file per preset, content is the JSON form of :class:`PresetRecord`.

Two distinct surfaces are served from the same record:

  * UI load: the operator picks a preset from the dropdown; the JS
    populates URL / mode-card / engine-radio / Goal / macro rows /
    code textarea verbatim. They can edit and submit normally.

  * API run: ``POST /presets/{name}/run`` builds a JobRequest
    from the SAVED options dict (the snapshot taken at save time)
    and submits it via the same code path as POST /jobs. Useful
    for cron / external schedulers that just want to fire off a
    pre-configured job.

Timestamps:
  - ``created_at``: first save (immutable across edits)
  - ``updated_at``: every save / edit
  - ``last_used_at``: when the preset was last loaded into the UI or
    triggered via /run. ``None`` until first use.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from server.hub._jsonstore import JsonRecordRegistry


def _safe_filename(name: str) -> str:
    """Turn an arbitrary preset name into a filesystem-safe slug.
    The original (unicode) name is kept inside the record; only the
    file basename is sanitised. Capped at 120 chars."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip().lower())
    slug = slug.strip("-")
    return (slug or "untitled")[:120]


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


@dataclass
class PresetRecord:
    """One saved Submit-form configuration.

    Fields fall into two groups:

      * UI snapshot -- ``ui_mode`` / ``ai_engine`` / ``goal`` /
        ``simple_rows`` / ``code_script`` / etc. Used by the JS to
        repopulate the form when a preset is loaded.

      * Run snapshot -- ``url`` + ``options`` (a plain JobOptions
        dict). Used by ``POST /presets/{name}/run`` to fire off
        a job without involving the UI compiler. The two snapshots
        are kept in sync at save time; if a user loads a preset,
        edits the macro rows, and re-submits without re-saving, the
        run snapshot stays at the previous version (consistent
        behavior across UI submit and API run is then up to the
        operator -- they save when they're happy).
    """

    name: str
    # Optional grouping label. The UI groups dropdown entries by
    # category. Empty string = "Uncategorised".
    category: str = ""
    # Optional one-line note shown next to the name in the dropdown.
    description: str = ""

    # ---- UI snapshot -----------------------------------------------
    ui_mode: str = "fetch"  # "fetch" | "ai" | "code"
    ai_engine: str = "codegen"  # "codegen" | "simple"
    url: str = ""
    # codegen-engine: the goal textarea content
    goal: str = ""
    # simple-engine: the macro rows; each = {"action": str, "detail": str}
    simple_rows: list[dict] = field(default_factory=list)
    # code-mode: the Python source the operator pasted in
    code_script: str = ""
    # numeric knobs that show up on the form
    max_attempts: int = 3
    attempt_timeout_s: int = 86400
    attempt_timeout_simple_s: int = 600
    host_dedup: bool = True

    # ---- Run snapshot ----------------------------------------------
    # The JobOptions dict that POST /presets/{name}/run will
    # forward as-is. Includes the resolved backend ``mode``
    # (codegen-loop / rerun / fetch / vision-agent) plus any
    # mode-specific keys (code, goal, max_codegen_attempts, etc.).
    # See server.protocol.JobOptions for the schema.
    options: dict = field(default_factory=dict)

    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> PresetRecord:
        return cls(
            name=str(d.get("name") or "").strip(),
            category=str(d.get("category") or "").strip(),
            description=str(d.get("description") or ""),
            ui_mode=str(d.get("ui_mode") or "fetch"),
            ai_engine=str(d.get("ai_engine") or "codegen"),
            url=str(d.get("url") or ""),
            goal=str(d.get("goal") or ""),
            simple_rows=list(d.get("simple_rows") or []),
            code_script=str(d.get("code_script") or ""),
            max_attempts=int(d.get("max_attempts") or 3),
            attempt_timeout_s=int(d.get("attempt_timeout_s") or 86400),
            attempt_timeout_simple_s=int(d.get("attempt_timeout_simple_s") or 600),
            host_dedup=bool(d.get("host_dedup", True)),
            options=dict(d.get("options") or {}),
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
            last_used_at=d.get("last_used_at"),
        )


class PresetRegistry(JsonRecordRegistry[PresetRecord]):
    """File-backed CRUD over the preset directory. Inherits the generic
    list / get / delete / atomic-write from :class:`JsonRecordRegistry`;
    only the preset-specific (de)serialisation + sort + the timestamp
    bookkeeping in :meth:`upsert` / :meth:`touch_used` live here."""

    subdir = "presets"

    # ---- JsonRecordRegistry hooks -----------------------------------------

    def _slug(self, key: str) -> str:
        return _safe_filename(key)

    def _key_of(self, rec: PresetRecord) -> str:
        return rec.name

    def _to_json(self, rec: PresetRecord) -> dict:
        return rec.to_json()

    def _from_json(self, d: dict) -> PresetRecord:
        return PresetRecord.from_json(d)

    def _sort_key(self, rec: PresetRecord):
        # Category-grouped (empty category last), then by name.
        return (rec.category == "", rec.category.lower(), rec.name.lower())

    # ---- preset-specific behaviour ----------------------------------------

    def upsert(self, rec: PresetRecord) -> PresetRecord:
        if not rec.name.strip():
            raise ValueError("preset name cannot be empty")
        existing = self.get(rec.name)
        now = _utcnow_iso()
        rec.created_at = existing.created_at if existing and existing.created_at else now
        rec.updated_at = now
        if existing and rec.last_used_at is None:
            rec.last_used_at = existing.last_used_at
        self._write(rec)
        return rec

    def touch_used(self, name: str) -> PresetRecord | None:
        """Bump ``last_used_at`` to now. Called both when the UI
        loads a preset and when POST /presets/{name}/run fires."""
        rec = self.get(name)
        if rec is None:
            return None
        rec.last_used_at = _utcnow_iso()
        self._write(rec)
        return rec

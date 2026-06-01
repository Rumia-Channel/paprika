"""GPU-bound job concurrency gate.

ぱっぷす環境では Qwen-VL を自前 RTX 6000 Pro Max-Q で走らせているため
サブスク料金はかからないが、**GPU 1 枚を 24 worker line で奪い合う** ので
codegen-loop (= page.agent / page.observe / page.ask を含み得る) ジョブが
複数同時に走ると GPU を完全飽和させて他の post-job perception や fetch
モードの軽量推論まで詰まる。

ここは GPU を食う codegen-loop ジョブの **同時実行数をクラスタ全体で制限**
するためのモジュール。in-memory のシンプルな counter set。hub 再起動で
リセットされるが、再起動時には全 running ジョブも止まる前提なので問題なし。

Usage:
    from server.hub._gpu_gate import (
        register_codegen_loop, unregister_codegen_loop,
        codegen_loop_at_capacity, codegen_loop_in_flight,
    )

    # dispatch 直前
    if codegen_loop_at_capacity():
        # 拒否 or grace ループで待機
        ...
    else:
        register_codegen_loop(job_id)
        ...

    # ジョブ完了時 (WorkerJobComplete / WorkerJobFailed の hub 側ハンドラ)
    unregister_codegen_loop(job_id)

env:
    PAPRIKA_CODEGEN_LOOP_CONCURRENCY (int, default 0 = unlimited)
        ぱっぷす 24-lane 本番では 3 を推奨。
"""
from __future__ import annotations

import os


_CODEGEN_LOOP_LIMIT = int(
    os.environ.get("PAPRIKA_CODEGEN_LOOP_CONCURRENCY", "0") or "0"
)

# job_id set. ジョブ単位で track するので同 job_id を 2 回 register しても
# 1 とカウントされる (idempotent)。WS 経由の重複 complete event 対策。
_codegen_loop_running: set[str] = set()


def get_codegen_loop_limit() -> int:
    """Return configured limit. 0 means unlimited."""
    return _CODEGEN_LOOP_LIMIT


def codegen_loop_in_flight() -> int:
    """Number of codegen-loop jobs currently registered as running."""
    return len(_codegen_loop_running)


def codegen_loop_at_capacity() -> bool:
    """Whether we should refuse to dispatch a new codegen-loop job now.

    False when the limit is 0 (= unlimited, default).
    """
    if _CODEGEN_LOOP_LIMIT <= 0:
        return False
    return len(_codegen_loop_running) >= _CODEGEN_LOOP_LIMIT


def register_codegen_loop(job_id: str) -> None:
    """Mark a codegen-loop job as running. Idempotent."""
    if job_id:
        _codegen_loop_running.add(job_id)


def unregister_codegen_loop(job_id: str) -> None:
    """Mark a codegen-loop job as no longer running. Idempotent.

    Safe to call from multiple completion paths (WorkerJobComplete /
    WorkerJobFailed / timeout reaper) without double-decrementing.
    """
    if job_id:
        _codegen_loop_running.discard(job_id)


def snapshot() -> dict:
    """Read-only snapshot for /health and admin UI."""
    return {
        "codegen_loop_limit": _CODEGEN_LOOP_LIMIT,
        "codegen_loop_running": len(_codegen_loop_running),
        "codegen_loop_jobs": sorted(_codegen_loop_running),
    }

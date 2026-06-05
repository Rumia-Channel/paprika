---
layout: doc
title: JobOptions — ジョブの全オプション
description: POST /jobs と SDK の options に渡せる全フィールドの完全リファレンス — モード(fetch / codegen-loop / rerun)、Fetch 系(待機・スクロール・動画・Cookie)、プロファイル、AI、rerun ソース、min_asset_size_bytes、fetch_strategy、keep_session、ハブ注入フィールドまで。
active: job-options
---

`POST /jobs` のリクエスト body `options`（SDK では `cli.fetch(url, **kwargs)` の追加引数 / `cli.session(url, options=...)` の dict）に渡せる全フィールドの**完全リファレンス**です。SDK の型定義（`server/protocol.py:JobOptions`）と 1 対 1 です。

> 投入経路の概要は [HTTP API](http-api.html)、SDK の使い方は [API リファレンス](api.html) を参照。

## 早見表

| カテゴリ | フィールド |
|---|---|
| **モード** | `mode`, `goal`, `max_codegen_attempts`, `attempt_timeout_s`, `rerun_from`, `code` |
| **Fetch 系（待機）** | `wait_seconds`, `settle_seconds`, `idle_seconds`, `max_wait_seconds`, `post_click_seconds` |
| **Fetch 系（スクロール）** | `scroll`, `scroll_step`, `scroll_max`, `scroll_early_after` |
| **動画 / アセット** | `download_video`, `capture_assets`, `min_asset_size_bytes` |
| **ブラウザ** | `headless`, `referer`, `cookies_from` |
| **プロファイル / アタッチ** | `use_profile`, `attach`, `clone_chrome_profile`, `attach_to_job` |
| **AI** | `codegen_engine` |
| **Fetch サブモード** | `fetch_strategy`, `fetch_recipe`（Hub 注入） |
| **セッション** | `keep_session` |

## モード

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `mode` | `"fetch" \| "codegen-loop" \| "rerun"` | `"fetch"` | 実行モード（[Hub の仕組み](architecture-hub.html#codegen-loop)） |
| `goal` | `str \| None` | `null` | `codegen-loop` の目標（自然言語）。**`codegen-loop` では必須** |
| `max_codegen_attempts` | `int` (1–10) | `3` | `codegen-loop` の生成→実行→再生成の試行回数 |
| `attempt_timeout_s` | `int` (30–864000) | `180` | 1 試行のサンドボックス実行タイムアウト（秒）。最大 **10 日** |
| `rerun_from` | `str \| None` | `null` | `rerun`: 既存ジョブ/アテンプトの script を再実行。形式: `"{job_id}"` / `"{job_id}/attempts/N"` |
| `code` | `str \| None` | `null` | `rerun`: インライン Python スクリプト（最大 **200 KB**） |

> **`rerun_from` と `code` は同時指定不可**。両方ある場合は `rerun_from` が勝ちます。

## Fetch 系（待機・スクロール）

`mode: "fetch"` のときに効きます。

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `wait_seconds` | `int` | `20` | ページ読み込み待ち（秒） |
| `settle_seconds` | `float` | `0.0` | ナビ後の落ち着き待ち（秒） |
| `idle_seconds` | `float` | `3.0` | ネットワーク無通信の判定（秒） |
| `max_wait_seconds` | `float` | `60.0` | 全体上限（秒） |
| `post_click_seconds` | `float` | `5.0` | クリック後の追加待ち（秒） |
| `scroll` | `bool` | `false` | 最後までスクロールして lazy-load も拾う |
| `scroll_step` | `int` | `50` | 1 回のスクロール量（px） |
| `scroll_max` | `int` | `3000` | スクロール上限（px） |
| `scroll_early_after` | `float` | `5.0` | スクロール早期終了の閾値（秒） |

## 動画 / アセット

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `download_video` | `bool` | `false` | iframe+ネスト iframe の通信トレースを ON + `yt-dlp` で動画取得。**`true` だと `capture_assets` も強制 `true`**（[動画の仕組み](video.html)） |
| `capture_assets` | `bool` | `true` | 取得したアセットをサーバ側に保存する |
| `min_asset_size_bytes` | `int` (≥0) | `0` | このサイズ未満のアセットを除外（`0` = 制限なし）。クライアントが `0` のままなら Hub の Settings 値で上書き |

## ブラウザ

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `headless` | `bool` | `false` | 画面を出さずに実行（Chrome `--headless`） |
| `referer` | `str \| None` | `null` | リクエストの Referer |
| `cookies_from` | `str \| None` | `null` | 指定ホストの Cookie を [Host レジストリ](host-recipe.html) から注入 |

## プロファイル / アタッチ

ログイン状態の持ち込み・既存ブラウザの再利用系。**4 つは同時指定不可**（最大 1 つ）。

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `use_profile` | `str \| None` | `null` | Hub に [アップロード済み](profile.html)のプロファイル名。省略時は既定プロファイルがあれば適用 |
| `attach` | `str \| None` | `null` | 既に走っている Chrome に接続。形式: `[HOST:]PORT` |
| `clone_chrome_profile` | `str \| None` | `null` | **ローカル限定**: 操作者の Chrome プロファイル名を tempdir にクローン（Hub と Chrome が同一ホストのときのみ） |
| `attach_to_job` | `str \| None` | `null` | 前ジョブのブラウザ Lane を再利用（同じ Chrome / user-data-dir → Cookie / ログインを維持） |

## AI（`codegen-loop`）

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `codegen_engine` | `str \| None` | `null` | `/engines` の slug。`kind=chat` または `vision-chat` + `protocol=openai` のもの。省略時は env 既定（`CODEGEN_LLM_URL` + `CODEGEN_MODEL_NAME`） |

## Fetch サブモード

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `fetch_strategy` | `"recipe" \| "normal"` | `"recipe"` | `recipe`: ホストにマッチするレシピを自動適用 / `normal`: スキップ（[Host レシピ](host-recipe.html)） |
| `fetch_recipe` | `dict \| None` | `null` | **Hub 注入**。`HostRegistry.pick_recipe` で見つかったレシピを Hub が差し込む。**API 側で直接セットしない** |

> AI 調査（管理画面の「AI で解析する」）は `fetch_strategy` の値ではなく、**`mode: "codegen-loop"`** で投入される別経路です。

## セッション

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `keep_session` | `bool` | `false` | **`fetch` のみ**。クロール終了後もブラウザ / セッションを残し、`JobInfo.session_id` の解決を続ける。noVNC で人手操作が可能。終了は `DELETE /sessions/{sid}` |

## 使用例

### Fetch（スクロール + 動画）

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url": "https://example.com",
  "options": {
    "mode": "fetch",
    "scroll": true,
    "scroll_max": 5000,
    "download_video": true,
    "min_asset_size_bytes": 2048
  }
}'
```

### codegen-loop（AI 駆動）

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url": "https://example.com",
  "options": {
    "mode": "codegen-loop",
    "goal": "メイン動画を再生してダウンロードして保存して",
    "max_codegen_attempts": 3,
    "attempt_timeout_s": 600
  }
}'
```

### rerun（保存済みスクリプトを再実行）

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url": "https://example.com",
  "options": {
    "mode": "rerun",
    "rerun_from": "abc123/attempts/2",
    "attempt_timeout_s": 600
  }
}'
```

### keep_session（人手で続きを触る）

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url": "https://example.com",
  "options": {"mode": "fetch", "keep_session": true}
}'
# -> 終わったら session_id が JobInfo に残るので noVNC で開ける
# 終わったら明示的に閉じる:
curl -X DELETE "$PAPRIKA_HUB/sessions/<session_id>"
```

## SDK からの渡し方

```python
async with paprika() as cli:
    # cli.fetch はキーワード引数として options を受け取る
    job = await cli.fetch(
        "https://example.com",
        scroll=True,
        download_video=True,
        min_asset_size_bytes=2048,
    )
```

## バリデーション

サーバ側で以下を強制します（違反は **422**）。

- `mode == "codegen-loop"` → `goal` 必須
- `mode == "rerun"` → `rerun_from` か `code` のいずれか必須
- `code` ≤ **200 KB**
- `max_codegen_attempts`: **1–10**
- `attempt_timeout_s`: **30 – 864000**（10 日）
- `min_asset_size_bytes`: **≥ 0**
- `download_video=true` のとき **`capture_assets` は自動で `true`** に強制

## 関連

- [HTTP API](http-api.html)
- [API リファレンス](api.html)
- [Hub の仕組み: 3 つのジョブモード](architecture-hub.html)
- [動画の仕組み](video.html)
- [Host レシピ](host-recipe.html) / [`use_profile`](profile.html)

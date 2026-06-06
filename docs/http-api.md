---
layout: doc
title: HTTP API（任意の言語から）
description: Python / PHP SDK を使わずに curl・JavaScript・Go など任意の言語から Paprika を叩く REST/JSON API ガイド。ジョブ投入 → 進捗ポーリング → アセット取得の流れ、503 リトライ、エラーコード一覧。
active: http-api
redirect_from:
  - /errors.html
---

Paprika SDK（Python / PHP）を使わなくても、Paprika は素の **HTTP / JSON API** で操作できます。`curl` でも JavaScript / Go / Ruby / どの言語からでも、やることは「**ジョブを投げて、結果を取る**」だけです。

> SDK の使い方は [API リファレンス](api.html)、概念は [Client インストール](intro.html) を参照。困ったら [FAQ](faq.html) へ。

## ベース URL と認証

すべてのエンドポイントは Hub のベース URL からの相対パスです。本ページの例では `http://localhost:8000`（環境変数 `PAPRIKA_HUB` を使う想定）。

```bash
export PAPRIKA_HUB=http://localhost:8000
```

既定では **認証なし**（Hub は private LAN 前提）。外部公開する場合は手前にリバースプロキシ + 認証を置いてください。

## 基本フロー（4 ステップ）

1. `POST /jobs` でジョブ投入 → `job_id` を受け取る
2. `GET /jobs/{job_id}` を **ポーリング** して `status` が終端になるのを待つ
3. `GET /jobs/{job_id}/assets.json`（または `/result`）で取得物の一覧を得る
4. 各アセットの `href` から **ダウンロード**

## 1. ジョブ投入 — `POST /jobs`

```bash
curl -X POST "$PAPRIKA_HUB/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","options":{"mode":"fetch","capture_assets":true}}'
```

レスポンス（`200`）:

```json
{ "job_id": "ccea8ea9dc2a", "status": "queued", "url": "https://example.com", "options": { } }
```

フリート（ワーカー）が満杯のときは **`503`** が返ります。Hub は在庫待ちせず即返すので、**クライアント側で指数バックオフ再試行**してください（[下記](#retry-503)）。

> ショートカット: `GET /<URL>` でも投入できます — `curl "$PAPRIKA_HUB/https://example.com"`

## 2. 進捗ポーリング — `GET /jobs/{id}`

```bash
curl "$PAPRIKA_HUB/jobs/ccea8ea9dc2a"
```

`status` は `queued` → `running` → **`completed` / `failed` / `cancelled`**（後ろ 3 つが終端）。1〜2 秒間隔のポーリングで十分です。

## 3. 取得物の一覧 — `GET /jobs/{id}/assets.json`

```json
{
  "job_id": "ccea8ea9dc2a",
  "count": 24,
  "items": [
    {
      "name": "06.jpg",
      "href": "/jobs/ccea8ea9dc2a/assets/06.jpg",
      "kind": "image",
      "mime": "image/jpeg",
      "size": 7160,
      "size_h": "7.0 KB",
      "ext": "jpg",
      "source_url": "https://.../06.jpg",
      "page_url": "https://..."
    }
  ]
}
```

`kind` は `image` / `video` / `audio` / `other`。
`GET /jobs/{id}/result` でも `html_href`・`log_href` + アセット一覧（やや簡易版）が得られます。

## 4. ダウンロード

`href` は Hub 相対パスなので、ベース URL を前置して取得します:

```bash
curl -O "$PAPRIKA_HUB/jobs/ccea8ea9dc2a/assets/06.jpg"
```

## まとめて：エンドツーエンドの例（bash）

```bash
#!/usr/bin/env bash
set -euo pipefail
HUB=${PAPRIKA_HUB:-http://localhost:8000}

# 1) 投入（503 は数回リトライ）
for i in 1 2 3 4 5; do
  resp=$(curl -s -o /tmp/j.json -w '%{http_code}' -X POST "$HUB/jobs" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com","options":{"mode":"fetch","capture_assets":true}}')
  [ "$resp" = "503" ] || break
  sleep $((i*2))
done
jid=$(python -c 'import json;print(json.load(open("/tmp/j.json"))["job_id"])')
echo "job=$jid"

# 2) 完了までポーリング
while :; do
  st=$(curl -s "$HUB/jobs/$jid" | python -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  echo "status=$st"; case "$st" in completed|failed|cancelled) break;; esac; sleep 2
done

# 3) アセットを全部ダウンロード
curl -s "$HUB/jobs/$jid/assets.json" \
  | python -c 'import sys,json;[print(i["href"]) for i in json.load(sys.stdin)["items"]]' \
  | while read -r href; do curl -sO "$HUB$href"; done
```

## AI に任せる（`mode: codegen-loop`）

URL と「やりたいこと（`goal`）」を渡すと、LLM がスクリプトを生成して実行します（未知サイト・複雑な操作・動画向け。LLM が走るので課金あり）:

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url": "https://example.com",
  "options": {"mode":"codegen-loop","goal":"メイン動画を再生してダウンロードして保存して","max_codegen_attempts":3}
}'
```

## ライブログ（WebSocket）

実行ログをリアルタイム受信できます:

```
ws://localhost:8000/jobs/{job_id}/events?since=0
```

各メッセージは JSON 1 行（`{"type":"log"|"done"|"error","data": ... }`）。

## その他

| メソッド・パス | 用途 |
|---|---|
| `GET /jobs` | ジョブ一覧 |
| `GET /jobs/{id}/page.html` | 取得した HTML |
| `GET /jobs/{id}/log.txt` | 実行ログ（全文） |
| `DELETE /jobs/{id}` | ジョブとファイルを削除 |
| `GET /workers` | ワーカー（フリート）状況 |

## 503 リトライ（重要） {#retry-503}

満杯時の `503` は正常な背圧です。必ずバックオフ再試行を入れてください:

```javascript
async function submit(body, hub = process.env.PAPRIKA_HUB) {
  for (let i = 0; i < 6; i++) {
    const r = await fetch(`${hub}/jobs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.status !== 503) return r.json();
    await new Promise(s => setTimeout(s, 1000 * 2 ** i)); // 1,2,4,8,16,32s
  }
  throw new Error('fleet busy — retried 6x');
}
```

## 主な `options`（よく使うもの）

| キー | 既定 | 説明 |
|---|---|---|
| `mode` | `fetch` | `fetch`（高速・レシピ）/ `codegen-loop`（AI）/ `rerun`（コード直接実行） |
| `download_video` | `false` | 通信トレース + yt-dlp で動画を取得 |
| `capture_assets` | `true` | 取得物をサーバに保存 |
| `scroll` | `false` | 最後までスクロールして遅延ロード（lazy）を拾う |
| `headless` | `false` | 画面を出さずに実行 |
| `min_asset_size_bytes` | `0` | これ未満のアセットを除外（`0` = 制限なし） |
| `use_profile` | `null` | アップロード済み Chrome プロファイル名 |
| `goal` | `null` | `codegen-loop` 時の目標（自然言語、必須） |
| `max_codegen_attempts` | `3` | `codegen-loop` の再試行回数 |
| `attempt_timeout_s` | `180` | 1 試行のタイムアウト（秒） |

**`options` の全フィールド・型・既定値・制約は [JobOptions リファレンス](job-options.html) に集約しています。**

## エラーコード一覧 {#errors}

Paprika の HTTP API が返すエラーコード（**ステータス**）と、それぞれの典型的な原因・**クライアント側でやるべきこと**をまとめます。SDK は多くの一過性エラー（特に `503`）を自動でリトライします。素の HTTP で叩く場合は自分で実装してください（[503 リトライ](#retry-503)）。

### 早見表

| コード | 意味 | クライアントの対応 |
|---|---|---|
| **400** Bad Request | リクエストのスキーマ違反 | リクエストを直す（再試行しない） |
| **404** Not Found | ジョブ / セッション / リソースが存在しない | 存在確認の見直し |
| **409** Conflict | 状態が操作と矛盾（例: 接続中の Worker を削除） | 状態を整えてから再実行 |
| **422** Unprocessable Entity | Pydantic バリデーション失敗 | フィールド値を直す |
| **502** Bad Gateway | Worker / ピア Hub への中継失敗 | しばらく置いて再試行 |
| **503** Service Unavailable | フリート満杯 / 一時的に受けられない | **指数バックオフで再試行**（必須） |
| **504** Gateway Timeout | アクション・転送のタイムアウト | タイムアウト見直し or 再試行 |

### 400 Bad Request — リクエストが不正

JSON スキーマの違反や、必須フィールドの欠落で出ます。**再試行しても直りません** — 内容を直してください。代表例:

- `"body must be a JSON object"`
- `"'url' must be a string"`
- `"codegen-loop mode requires 'goal'"`（`mode: "codegen-loop"` で `goal` が無い）
- `"'actions' must be a list of action dicts"`（Host レシピの形式違反）
- `"invalid profile name"`（プロファイル名の文字種違反）

`400` の `detail` に**理由がそのまま入ります**。`curl` なら `-i` を付けて確認してください。

### 404 Not Found — 存在しない

```json
{ "detail": "job not found" }
```

- ジョブ ID / セッション ID が間違っている
- ジョブを `DELETE` した後の参照
- セッションが TTL で reap された後の参照（[アーキテクチャ → Worker](architecture.html#worker)）
- アセットファイル名のタイポ

### 409 Conflict — 状態の矛盾

操作と現在の状態が噛み合いません。

- **接続中の Worker を `DELETE /workers/{id}` しようとした** — 先に `status=drain` にして切断する
- セッションがまだクロージング中

### 422 Unprocessable Entity — Pydantic バリデーション失敗

[JobOptions](job-options.html) の型・範囲制約で落ちたとき:

```json
{
  "detail": [
    {"loc": ["body", "options", "max_codegen_attempts"], "msg": "ensure this value is less than or equal to 10"}
  ]
}
```

代表的な制約（[JobOptions リファレンス](job-options.html) より）:

| フィールド | 制約 |
|---|---|
| `max_codegen_attempts` | 1 ≤ n ≤ 10 |
| `attempt_timeout_s` | 30 ≤ n ≤ 864000（10 日） |
| `code` | ≤ 200 KB |
| `min_asset_size_bytes` | ≥ 0 |
| `mode = "codegen-loop"` | `goal` 必須 |
| `mode = "rerun"` | `rerun_from` か `code` のいずれか必須 |

### 502 Bad Gateway — 中継失敗

Hub から **Worker** や **ピア Hub** への中継が失敗したときに出ます。

- `worker '...' is no longer connected`（Worker が接続切れ）
- `hub forward to '...' failed: ...`（マルチ Hub 構成でピア Hub への転送が失敗）
- `screenshot send failed: ...`

ほとんどが**一過性**なので、しばらく置いて再試行で直ります。続くようなら管理画面で Worker / ピア Hub の状態を確認してください。

### 503 Service Unavailable — フリート満杯

**最も重要なエラー**です。Hub は満杯時に在庫待ちせず即 `503` を返します。実装例とリトライ戦略は上の [503 リトライ](#retry-503) を参照。

### 504 Gateway Timeout — タイムアウト

- `session action timed out`（セッション操作が時間切れ）
- Worker への RPC タイムアウト

短時間の再試行で直ることもあります。長すぎるアクションは [`attempt_timeout_s`](job-options.html#mode) を伸ばすか、処理を分割してください。

### トラブルシュートの流れ

1. **レスポンスボディの `detail`** を読む（理由が日本語または英語で入っています）
2. `400` / `422` なら**入力を直す**（リトライしない）
3. `503` なら**指数バックオフ再試行**
4. `404` なら**ID を確認**（ジョブが消えていないか、セッションが reap されていないか）
5. `409` なら**状態を整える**（drain にしてから削除など）
6. `502` / `504` は**しばらく待って再試行**
7. それでも続く場合は管理画面の **Live パネル**（ジョブ単位のログ）や **Workers タブ**で状況を確認

### SDK で受け取る

SDK は HTTP エラーを Python 例外にマッピングします。

```python
from paprika_client.errors import (
    JobSubmitError,        # 400 / 422 / 503 投入時
    JobTimeoutError,       # クライアント側ポーリング上限超過
    PaprikaActionError,    # Page/Locator/Session 操作の失敗
)

try:
    job = await cli.fetch("https://...", scroll=True)
except JobSubmitError as e:
    print("投入失敗:", e)
except JobTimeoutError:
    print("時間内に終わらず")
```

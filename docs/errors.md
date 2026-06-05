---
layout: doc
title: エラーリファレンス（HTTP ステータス）
description: Paprika の HTTP API が返すエラーコード一覧 — 400/404/409/422/502/503/504 の意味、典型的な発生条件、クライアント側でやるべきこと(リトライ・修正・諦め)。
active: errors
---

Paprika の HTTP API が返すエラーコード（**ステータス**）と、それぞれの典型的な原因・**クライアント側でやるべきこと**をまとめます。

> SDK は多くの一過性エラーを **自動でリトライ**します（特に `503`）。素の HTTP で叩く場合は自分で実装してください（[HTTP API: 503 リトライ](http-api.html#retry-503)）。

## 早見表

| コード | 意味 | クライアントの対応 |
|---|---|---|
| **400** Bad Request | リクエストのスキーマ違反 | リクエストを直す（再試行しない） |
| **404** Not Found | ジョブ / セッション / リソースが存在しない | 存在確認の見直し |
| **409** Conflict | 状態が操作と矛盾（例: 接続中の Worker を削除） | 状態を整えてから再実行 |
| **422** Unprocessable Entity | Pydantic バリデーション失敗 | フィールド値を直す |
| **502** Bad Gateway | Worker / ピア Hub への中継失敗 | しばらく置いて再試行 |
| **503** Service Unavailable | フリート満杯 / 一時的に受けられない | **指数バックオフで再試行**（必須） |
| **504** Gateway Timeout | アクション・転送のタイムアウト | タイムアウト見直し or 再試行 |

## 400 Bad Request — リクエストが不正

JSON スキーマの違反や、必須フィールドの欠落で出ます。**再試行しても直りません** — 内容を直してください。

代表例（実コードから）:

- `"body must be a JSON object"`
- `"'url' must be a string"`
- `"codegen-loop mode requires 'goal'"`（`mode: "codegen-loop"` で `goal` が無い）
- `"'actions' must be a list of action dicts"`（Host レシピの形式違反）
- `"invalid profile name"`（プロファイル名の文字種違反）

> `400` の `detail` に**理由がそのまま入ります**。`curl` なら `-i` を付けて確認するか、エラーログから拾ってください。

## 404 Not Found — 存在しない

```json
{ "detail": "job not found" }
```

- ジョブ ID / セッション ID が間違っている
- ジョブを `DELETE` した後の参照
- セッションが TTL で reap された後の参照（[Worker・Lane の仕組み](architecture-worker.html)）
- アセットファイル名のタイポ

## 409 Conflict — 状態の矛盾

操作と現在の状態が噛み合いません。

- **接続中の Worker を `DELETE /workers/{id}` しようとした** — 先に `status=drain` にして切断する
- セッションがまだクロージング中

## 422 Unprocessable Entity — Pydantic バリデーション失敗

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

## 502 Bad Gateway — 中継失敗

Hub から **Worker** や **ピア Hub** への中継が失敗したときに出ます。

- `worker '...' is no longer connected`（Worker が接続切れ）
- `hub forward to '...' failed: ...`（マルチ Hub 構成でピア Hub への転送が失敗）
- `screenshot send failed: ...`

ほとんどが**一過性**なので、しばらく置いて再試行で直ります。続くようなら管理画面で Worker / ピア Hub の状態を確認してください。

## 503 Service Unavailable — フリート満杯

**最も重要なエラー**です。Hub は満杯時に在庫待ちせず即 `503` を返します。

```json
{ "detail": "no worker available (fleet at capacity); retry with backoff" }
```

クライアントは **必ず指数バックオフ再試行**を入れてください（SDK は自動）:

```javascript
async function submit(body, hub) {
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

詳細は [HTTP API: 503 リトライ](http-api.html#retry-503)、構成は [Hub の仕組み](architecture-hub.html) と [Hub スケーリング](scaling.html) を参照。

## 504 Gateway Timeout — タイムアウト

- `session action timed out`（セッション操作が時間切れ）
- Worker への RPC タイムアウト

短時間の再試行で直ることもあります。長すぎるアクションは [`attempt_timeout_s`](job-options.html#mode) を伸ばすか、処理を分割してください。

## トラブルシュートの流れ

1. **レスポンスボディの `detail`** を読む（理由が日本語または英語で入っています）
2. `400` / `422` なら**入力を直す**（リトライしない）
3. `503` なら**指数バックオフ再試行**
4. `404` なら**ID を確認**（ジョブが消えていないか、セッションが reap されていないか）
5. `409` なら**状態を整える**（drain にしてから削除など）
6. `502` / `504` は**しばらく待って再試行**
7. それでも続く場合は管理画面の **Live パネル**（ジョブ単位のログ）や **Workers タブ**で状況を確認

## SDK で受け取る

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

## 関連

- [HTTP API](http-api.html)
- [JobOptions](job-options.html) — バリデーション制約
- [FAQ](faq.html) — よくあるハマりどころ
- [Hub の仕組み](architecture-hub.html) — `503` の背圧設計

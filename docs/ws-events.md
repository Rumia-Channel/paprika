---
layout: doc
title: WebSocket — ライブログ /jobs/{id}/events
description: ジョブの実行ログをリアルタイムで受信する WebSocket /jobs/{id}/events の仕様 — メッセージ型(log/done/error)、since= による途中接続、再接続戦略、JS/Python の実装例。
active: ws-events
---

実行中のジョブのログを **リアルタイムで受信**する WebSocket です。完了を待つだけならポーリング（[HTTP API](http-api.html)）で十分ですが、**長時間ジョブ**や**ライブ表示**にはこちらが向きます。

## 接続

```
ws://localhost:8000/jobs/{job_id}/events?since=N
wss://your-hub.example/jobs/{job_id}/events?since=N
```

- `since=N`（任意、整数）: ログを **N 行目から**送ってほしい。再接続時に使う（[後述](#reconnect)）。既定 `0`。
- 認証: 既定で**認証なし**（private LAN 前提）。外部公開時は手前にリバプロを。

## メッセージの形

各メッセージは **JSON 1 行**。共通形は `{ "type": <kind>, "data": { ... } }`。

| `type` | `data` | 意味 |
|---|---|---|
| `log` | `{ "line": "<text>" }` | ログ行が 1 つ流れた |
| `done` | `{ "status": "completed" \| "failed" \| "cancelled" }` | ジョブが終端に達した（この後 WS は閉じる） |
| `error` | `{ "message": "<reason>" }` | サーバ側のエラー（接続時 `job not found` 等） |

`status` の値は `GET /jobs/{id}` の `status` と同じ意味です。

## 最小例（JavaScript / ブラウザ）

```javascript
const ws = new WebSocket(`ws://localhost:8000/jobs/${jobId}/events?since=0`);
ws.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  if (ev.type === "log")   console.log(ev.data.line);
  if (ev.type === "done")  console.log("ended:", ev.data.status);
  if (ev.type === "error") console.warn("error:", ev.data.message);
};
ws.onclose = () => console.log("disconnected");
```

## 最小例（Python / `websockets`）

```python
import asyncio, json, websockets

async def tail(job_id: str):
    url = f"ws://localhost:8000/jobs/{job_id}/events?since=0"
    async with websockets.connect(url) as ws:
        async for raw in ws:
            ev = json.loads(raw)
            if ev["type"] == "log":
                print(ev["data"]["line"])
            elif ev["type"] == "done":
                print("ended:", ev["data"]["status"])
                break

asyncio.run(tail("ccea8ea9dc2a"))
```

## 再接続戦略 {#reconnect}

ネットワークが切れたら **指数バックオフで再接続** し、**`since=<受信済み行数>`** で続きから受け取ります。サーバは過去のログを保持しているので **抜けなく追従**できます。

```javascript
let seen = 0;
function connect() {
  const ws = new WebSocket(`ws://localhost:8000/jobs/${jobId}/events?since=${seen}`);
  let backoff = 1000;
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "log") { seen++; render(ev.data.line); }
    if (ev.type === "done") { ws.close(); finalize(ev.data.status); }
  };
  ws.onopen  = () => { backoff = 1000; };
  ws.onclose = () => {
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 15000);   // 1s, 2s, 4s, ..., 15s 上限
  };
}
connect();
```

ポイント:

- `since=` を **クライアント側で必ず保持**してから再接続。
- `done` を受け取ったら**再接続しない**（次は出ません）。

## サーバ側の動き（参考）

- ハブはジョブごとに**過去ログを保持**しており、新規 WS 接続時に `since` 以降をまとめて送ったあと、以降は新しい行を**即座にプッシュ**します。
- ジョブが既に終了している場合は、`done` をすぐ送って閉じます。`status` で結果が分かります。
- 完了直前の最終ログが**消えないように**、`done` の直前に残バッファを flush します。

## ログ以外（管理画面向け）

`type: log` のうち、いくつかの**特殊プレフィックス**でハブが付け足す行があります（管理画面の Live パネル用、操作者向け）:

| プレフィックス | 用途 |
|---|---|
| `[[paprika:progress]] {...}` | ダウンロード進捗（プログレスバーに表示） |
| `[[paprika:netcap]] {...}` | 通信トレース・キャプチャの増分 |
| `[[paprika:asset]] ...` | アセットが 1 件追加されたサイン |
| `[[paprika:links]] ...` | リンク一覧が更新されたサイン |

これらは管理画面では UI に反映されますが、自前のロガーで読むときは **`startswith("[[paprika:")` で除外**するのがおすすめです。

## いつ使う?

| やりたいこと | 推奨 |
|---|---|
| 終了を待ちたいだけ | `GET /jobs/{id}` のポーリング（[HTTP API](http-api.html)） |
| 長時間ジョブをライブで見たい | **このページ** |
| ダウンロード進捗を出したい | **このページ**（`[[paprika:progress]]` を読む） |
| ブラウザ画面そのものを見たい | [VNC 埋め込み](vnc-embed.html) |

## 関連

- [HTTP API](http-api.html)
- [API リファレンス](api.html)
- [管理画面ガイド](admin.html)

# Hub マルチプロセスアーキテクチャ設計

Worker 200台（Lane 400〜1600本）に対応するための Hub 水平スケール設計。

## 現状の問題

Hub は単一 asyncio プロセス。Worker 200台が各10秒の heartbeat + 同時稼働ジョブからの
WorkerJobLog（数千 msg/sec）+ noVNC プロキシ + セッション RPC を1プロセスで捌く。

ボトルネック:
- **WorkerJobLog**: 1行1WSメッセージ → Hub側で Redis RPUSH + PUBLISH = 数千ops/sec
- **Session RPC**: WS は fd-bound、別プロセスからは Worker に送れない
- **codegen-loop**: LLM呼び出し + Docker 起動がイベントループを圧迫

## トポロジ

```
                     ┌────────┐
  Workers ──WS──→    │ nginx  │ ──sticky──→  Hub #1  (WS + HTTP)
  Workers ──WS──→    │ (L7)   │ ──sticky──→  Hub #2  (WS + HTTP)
  Workers ──WS──→    │        │ ──sticky──→  Hub #3  (WS + HTTP)
  Admin UI ──HTTP──→ │        │ ──round-robin─→ any Hub
                     └────┬───┘
                          │
                     ┌────▼───┐
                     │ Redis  │  (既存)
                     └────────┘
```

- **Hub プロセス**: 2〜4台。各プロセスが FastAPI 全機能を持つ（WS + HTTP 両方）
- **nginx**: L7 リバースプロキシ。Worker WS は sticky、HTTP API は round-robin
- **Redis**: 既存。プロセス間通信バスとして拡張利用
- Worker 側の変更: **なし**（nginx 経由で同じ URL に接続）

### 専用 WS Gateway を作らない理由

`_handle_worker_message` (workers.py L984-1344) はジョブ状態遷移、プロファイル同期、
セッション reconciliation を含む360行のハンドラ。分離するとほぼ全ロジックの複製が必要。
Worker 200台 × heartbeat 10秒 = 20 msg/sec — asyncio にとって軽い。
Hub 3台で分散すれば1台あたり67 Worker、十分。

## Redis チャネル/キー設計

### Worker メタデータ（既存を拡張）

```
paprika:worker:{id}             STRING  JSON (capabilities, in_flight) — 既存
paprika:worker:{id}:online      STRING  "1" EX 120s — 既存
paprika:worker:{id}:counts      HASH    in_flight, capacity — 既存
paprika:worker:{id}:hub_id      STRING  "hub-{uuid}"          ← 新規
paprika:worker:{id}:meta        HASH    client_address,        ← 新規
                                        public_base_url,
                                        status (active/drain/standby),
                                        lane_novnc_urls (JSON)
```

### セッションレジストリ（in-memory → Redis）

```
paprika:session:{sid}             HASH   worker_id, lane_idx, novnc_url,
                                         initial_url, created_at, last_active_at,
                                         idle_ttl_s, absolute_ttl_s, job_id,
                                         state, detached
paprika:sessions                  SET    全アクティブ session_id
paprika:sessions:worker:{wid}    SET    Worker 別セッション索引
```

### クロスプロセス Job ディスパッチ

```
paprika:hub:dispatch:{worker_id}  PUB/SUB  HubAssignJob JSON
```

### クロスプロセス Session RPC

```
paprika:rpc:req:{worker_id}       PUB/SUB  {request_id, type, payload}
paprika:rpc:res:{request_id}      PUB/SUB  {request_id, result}
```

## フロー詳細

### 1. Job ディスパッチ

```
POST /jobs → Hub #2
  → pick_worker_global()
    → 1st: ローカル connections から探す（今と同じ）
    → 2nd: Redis から全 Worker の capacity を読む
  → Worker がローカル → 直接 worker.send(HubAssignJob)
  → Worker がリモート → PUBLISH paprika:hub:dispatch:{worker_id}
                        Hub #1 が受信 → worker.send(HubAssignJob)
```

ジョブ投入は fire-and-forget。結果は RedisJobStore 経由で取得（既に対応済み）。

### 2. Session RPC（クリック等）

```
POST /sessions/{sid}/click → Hub #3
  → Redis HGET paprika:session:{sid} → worker_id
  → Worker がローカル → 直接 worker.session_action() — 今と同じ
  → Worker がリモート:
    1. request_id = uuid4()
    2. SUBSCRIBE paprika:rpc:res:{request_id}
    3. PUBLISH paprika:rpc:req:{worker_id}
       {request_id, type: "session_action", session_id, action}
    4. Hub #1 が受信 → worker.session_action() → 結果取得
    5. Hub #1: PUBLISH paprika:rpc:res:{request_id} {result}
    6. Hub #3 が受信 → HTTP レスポンス返却
  → タイムアウト: 30秒（既存と同じ）
```

同じパターンで screenshot, session_start, session_end, session_agent も処理。

### 3. noVNC プロキシ

変更不要。`_resolve_session_novnc_target()` がセッション→ (host, port) を解決して
直接 TCP 接続。どの Hub プロセスからでも可能。

必要データ: Worker の `client_address` を Redis に書く（`paprika:worker:{id}:meta`）。

### 4. codegen-loop

変更不要。`POST /jobs` を受けた Hub プロセスがそのまま asyncio.Task で実行。
`_local_sem` はプロセスローカルのまま（Hub 3台 × max_concurrent_jobs=2 = 合計6並列）。
Runner コンテナは nginx 経由で `/sessions` API を叩く。

### 5. WorkerJobLog バッチ化

Hub 側に `LogBatcher` を追加:

```python
class LogBatcher:
    """100ms or 50行 でフラッシュ。Redis pipeline で一括書き込み。"""

    async def add(self, job_id: str, line: str): ...
    async def _flush(self, job_id: str): ...
      # pipeline: RPUSH + PUBLISH (改行結合のバッチ)
```

効果: Redis ops 5000/sec → ~100/sec。ログ遅延 +100ms（人間には不可視）。

## 変更対象ファイル

| ファイル | 変更内容 |
|---|---|
| `server/hub/sessions.py` | `RedisSessionRegistry` 追加。async インターフェース |
| `server/scheduler.py` | `pick_worker_global()`, Worker meta を Redis に拡張書き込み |
| `server/hub/routes/sessions.py` | `_send_session_action` にリモート Worker RPC パス追加 |
| `server/hub/routes/workers.py` | `worker_link` にディスパッチ/RPC リスナー起動追加 |
| `server/hub/_rpc.py` | **新規** — Redis pub/sub RPC ヘルパー |
| `server/hub/_log_batcher.py` | **新規** — ログバッチ化 |
| `server/store.py` | `LogBatcher` 統合 |
| `server/hub/app.py` | lifespan で `RedisSessionRegistry` 初期化 |
| `docker-compose.yml` | nginx 追加、Hub レプリカ設定 |

## マイグレーションフェーズ

### Phase 0: 準備（動作変更なし）

- `SessionRegistry` のメソッドを `async def` に変更（呼び出し側 ~30箇所に `await` 追加）
- `WorkerRegistry.register()` / `heartbeat()` で `client_address`, `status`, `lane_novnc_urls` を Redis に書く
- `pick_worker_global()` スタブ追加（ローカルにフォールスルー）
- **テスト**: 単一プロセスで動作が変わらないことを確認

### Phase 1: Redis SessionRegistry

- `RedisSessionRegistry` 実装
- `make_session_registry(redis_client)` ファクトリ
- lifespan で切り替え
- セッション reaper を Redis 対応（SMEMBERS + パイプライン HGETALL）
- **テスト**: Hub 再起動後にセッションが生き残る

### Phase 2: グローバル Worker ディスパッチ

- `pick_worker_global()` の Redis 読み取り実装
- ディスパッチ用 pub/sub チャネル購読（`worker_link` 内で `asyncio.create_task`）
- ジョブ投入のリモートパス追加
- **テスト**: Hub #1 に POST /jobs → Hub #2 の Worker にディスパッチされる

### Phase 3: クロスプロセス Session RPC

- `server/hub/_rpc.py` 実装（request/response pub/sub パターン）
- `_send_session_action` をリモート対応
- `create_session`, `close_session`, `session_agent`, `request_screenshot` も同様
- RPC リスナータスク起動（`worker_link` 内）
- 分散ロック（Redis SETNX、セッション単位）
- **テスト**: SDK から session action → リモート Worker で実行される

### Phase 4: マルチプロセスデプロイ

- nginx 設定追加（sticky WS + round-robin HTTP）
- `docker-compose.yml` に Hub レプリカ + nginx 追加
- Worker の接続先を nginx に変更
- **テスト**: Worker 200台で負荷試験

### Phase 5: ログバッチ化 + 堅牢化

- `LogBatcher` 実装・統合
- ファイルベースレジストリの排他制御（Redis SETNX or flock）
- Hub プロセス別ヘルスチェック / メトリクス
- **テスト**: ログ遅延が 100ms 以内、Redis ops が 1/50 に削減

## 後方互換性

- `--redis-url` なし: 全て in-memory フォールバック（今と同じ単一プロセス動作）
- `--redis-url` あり + Hub 1台: リモートパスは通らない（ローカル Worker のみ）
- `--redis-url` あり + Hub N台: 全機能有効

Worker 側の変更は **一切不要**。

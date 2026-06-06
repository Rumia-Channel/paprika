---
layout: doc
title: 用語集
description: Paprika の中核用語 (Hub / Worker / Lane / Session / Job / Mode / Locator / walk / use_profile / Bridge / codegen-loop / fetch / rerun / AI mode / 管理画面) を 1 ページで定義。
active: glossary
---

Paprika 全体で繰り返し出てくる中核用語を 1 ページで定義します。
各用語の **詳細リンク** から、その概念をメインで扱うページへ飛べます。

## アーキテクチャ

### Hub（ハブ）
クライアントからのジョブを受け付け、フリート上のワーカーへ分配する中央サーバー。
HTTP API・管理画面・WebSocket（ライブログ）を提供します。
1 つの Hub が `localhost:8000` で動くシンプル構成から、複数 Hub を nginx で
水平スケールする本番構成まで対応。

[詳細 → アーキテクチャ概要](architecture.html) / [サーバー構成](operations.html#patterns)

### Worker（ワーカー）
Chrome ブラウザを実際に動かす実行ホスト。Hub と WebSocket で常時接続して
ジョブを受け取ります。1 worker = 1 マシン（VM / コンテナ）が一般的。
各 worker は **複数の Lane** を持ち、並列に複数のブラウザを動かせます。

[詳細 → Worker 自己回復](worker-resilience.html)

### Lane（レーン）
1 worker 上で並列稼働する **独立した Chrome インスタンス** 1 本。
worker N 台 × Lane M 本 = 全体で N×M のブラウザが並列実行可能。
既定は 1 worker あたり 2 Lanes（環境変数 `LANE_POOL` で調整）。
各 Lane は専用の Xvfb + Chrome + noVNC で構成されます。

[詳細 → Hub スケーリング](scaling.html)

### Fleet（フリート）
全 worker × 全 Lane の総体を指す呼称。
「フリート全体で 50 Lane が動いている」のような使い方をします。

---

## ジョブ・実行モード

### Job（ジョブ）
クライアントが Hub に投入する **1 つの処理単位**。
URL（または起点）+ 実行モード + オプションで定義され、完了時にアセット
（画像・動画・HTML）と `meta.json` を返します。`POST /jobs` で投入、
`GET /jobs/{id}` で状態確認、`WS /jobs/{id}/events` でライブログ追跡。

[詳細 → HTTP API](http-api.html) / [JobOptions](job-options.html)

### Mode（モード）
ジョブの実行方式の選択肢。

| モード | 使い所 | 特徴 |
|---|---|---|
| **`fetch`** | 既知サイトを既定の挙動で巡回 | 速い・LLM 不使用・決定的 |
| **`codegen-loop`** | 未知サイト / 複雑な操作 / 動画 | LLM がスクリプトを生成・実行・失敗時に再生成 |
| **`rerun`** | 成功した script を再利用 | 確定的・LLM 不使用 |

迷ったら `fetch` で試し、うまく拾えなければ AI モード (`codegen-loop`) に
上げるのが定石。

[詳細 → FAQ: モード選択](faq.html#どのモードを使えばいい)

### codegen-loop（AI モード）
`mode: codegen-loop` の通称。`goal` フィールドに自然言語で指示すると LLM が
スクリプトを生成・実行・失敗時に再生成するループ。成功した script は
**そのまま `mode: rerun` で再利用** できる（次回からは LLM 不要・決定的）。

[詳細 → AI エンジン追加ガイド](engine-setup.html)

### fetch（既定の収集モード）
既知サイト向けの「ページを開いてアセットを収集」するシンプルなモード。
スクロール（`options.scroll`）・最小サイズ（`min_asset_size_bytes`）等の
オプションで挙動を調整。LLM は使わず、最も速い。

### rerun（保存済み script の再実行）
`codegen-loop` で成功した script を `mode: rerun` で再投入することで、
同じ手順を **決定的に** 何度でも実行できる。LLM 不要 = 無料。

---

## API・概念

### Session（セッション）
クライアントから明示的に開く「ブラウザのタブ」相当のオブジェクト。
`async with cli.session(url) as page:` の `page` がそれです。
ブロックを抜けると自動で閉じる（lane が解放される）。
Session は **Page を継承** するので、`page.click()` などはすべて
そのまま `session.click()` でも呼べる。

[詳細 → API リファレンス: Session](api.html#session)

### Page（ページ）
Playwright スタイルの操作 API を提供するオブジェクト。
`goto` / `click` / `fill` / `evaluate` / `screenshot` / `links` …。
通常は `cli.session()` で生成、Session として使用。

[詳細 → API リファレンス: Page](api.html#page)

### Locator（ロケータ）
要素を「**こういう特徴の要素**」と宣言的に指定する方法。
セレクタ文字列より頑健で、Playwright スタイルの **チェーン** で書ける。
`page.get_by_role('button', name='送信').click()` のような形。

[詳細 → Locator リファレンス](locator.html)

### walk（巡回）
1 つの起点 URL から **BFS / DFS** でサイト内を巡回する機能。
`cli.walk(start_url, options=...)` または `mode: walk` でジョブ化。
深さ・件数・URL パターンで絞り込みできる。

[詳細 → walk リファレンス](walk.html)

### data-paprika-id（@N）
ページ DOM の各要素に Paprika が振る一意な ID（`outline()` が返すテキストの
`[@N]`）。LLM が生成するスクリプトでもこの ID で要素を参照するため、
レイアウトが変わってもセレクタが壊れにくい。

```python
await page.click('[data-paprika-id="42"]')
```

### Asset（アセット）
ジョブ実行中にブラウザが読み込んだ画像・動画・HTML などの **副産物**。
別途 URL を叩いて再取得するのではなく、ブラウザが実際に読み込んだバイト列を
そのまま回収するため、Cookie / Referer / 認証が必要な画像も取りこぼさない。

[詳細 → なぜ Paprika?](why-paprika.html)

---

## ログイン継続・プロファイル

### Bridge（Paprika Bridge 拡張）
普段使いの Chrome に入れる軽量拡張。`chrome.cookies` API でログイン Cookie を
Hub に push し、Paprika の Chrome 側で再利用する仕組み。
Chrome 127+ の **v20 App-Bound Encryption** にも対応（CLI の Cookie 復号は
レガシー v10 のみで非対応）。

[詳細 → Bridge 拡張](auth.html#bridge)

### use_profile
普段使いの Chrome の **User Data フォルダをまるごと持ち込む** 機能。
Cookie / 保存パスワード / autofill / 拡張機能まで一式アップロードして、
ログイン済みの状態でジョブを開始できる。

[詳細 → use_profile](auth.html#use-profile)

### Host レシピ
特定ホストに対する **自動化設定** を Hub に登録する仕組み。
例: 「`example.com` に来たら自動でログインボタンを押す」「Cookie 同意ダイアログを
自動で閉じる」など、サイト固有の決まり仕事を毎ジョブで再実行不要に。

[詳細 → Host レシピ](auth.html#host-recipe)

### paprika-agent 拡張
Paprika 自体が同梱する Chrome 拡張。`userScripts` 権限で **任意の JS を全ページに
常駐注入** したり、`declarativeNetRequest` でリクエストヘッダを書き換えたりと、
CDP 単独では届かない領域までを操作可能にする。
（Bridge 拡張とは別物。Bridge は普段使い Chrome に入れる、agent は Paprika の
Chrome に入る。）

---

## その他

### 管理画面 (Admin UI)
Hub に内蔵された Web UI。`http://your-hub.example:8000` で開き、ブラウザだけで
URL を投げて収集できる（コード不要）。実行中のジョブを **noVNC で目視**確認・
ログ追跡・アセット閲覧。

[詳細 → 管理画面ガイド](admin.html)

### noVNC
Chrome の画面を **Web ブラウザでリアルタイム閲覧・操作** できる仕組み。
各 Lane に紐づく専用 URL があり、管理画面の「Live パネル」から開く。
人手による引き継ぎや自前ページへの埋め込みも可能。

[詳細 → VNC 埋め込み](vnc-embed.html)

### nodriver
Paprika が採用している Chrome 操作ライブラリ。`navigator.webdriver` などの
典型的な自動化シグナルを出さないため、検出されにくい起動が可能。

### vLLM / OpenAI 互換エンドポイント
AI モード (`codegen-loop` / `page.agent` / R1 judge / distiller) が利用する
LLM 推論エンドポイント。OpenAI 互換 API なら何でも接続可能（vLLM・llama.cpp・
Ollama・Anthropic 等）。`fetch` モードだけ使うなら **不要**。

[詳細 → AI エンジン追加ガイド](engine-setup.html)

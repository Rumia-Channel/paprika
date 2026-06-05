---
layout: doc
title: walk — サイト巡回(BFS / DFS)
description: Paprika の walk() / Walker でサイトを BFS/DFS で巡回する完全リファレンス — start_url / target_pages / same_domain / allowed_domains / allow_paths / deny_paths / order / max_depth / per_page_timeout_s / persist_state / host_dedup / recrawl_patterns、Visit のフィールド、ベストプラクティス。
active: walk
---

`walk()` はサイトを **BFS / DFS** で巡回しながら 1 ページずつ `Visit` を yield する**非同期イテレータ**です。キュー・重複除去・ドメインとパスのフィルタ・オフスコープ redirect 対応まで内部で処理するので、自前の BFS ループより堅牢です。

> 全体像は [Client インストール](intro.html) / [ガイド](guides.html#walk) / 目的別の例は [ユースケース](usecases.html)。

## 最小例

```python
import asyncio
from paprika_client import paprika, walk

async def main():
    async with paprika() as cli:
        async with cli.session("https://example.com", parent_job_id="crawl") as page:
            async for visit in walk(page, target_pages=50, same_domain=True):
                print(f"[{visit.n}/{visit.target}] depth={visit.depth} {visit.url}")
                await page.save_assets("out/images")

asyncio.run(main())
```

- `walk(page, **opts)` は `Visit` を yield する **AsyncIterator**。クラス版は `Walker`（ステートを覗きたいとき）。
- ループ中で `await page.save_assets(...)` などを呼べば、そのページのアセットを保存できます。

> **`walk` は async のみ**。`sync_paprika` から直接は使えません（`asyncio.run()` でラップしてください）。

## オプション一覧（完全版）

| 引数 | 型 | 既定 | 意味 |
|---|---|---|---|
| `start_url` | `str` | 現在 URL | 巡回の起点。**絶対 URL** が必須（省略時は `page.url` から取る） |
| `target_pages` | `int` | `100` | 訪問ページ数の上限（達したら停止） |
| `same_domain` | `bool` | `True` | 開始 URL と**同じドメイン**だけを巡回 |
| `allowed_domains` | `Iterable[str] \| None` | `None` | 明示的に許可するドメインのリスト（指定すると `same_domain` より優先） |
| `allow_paths` | `Iterable[str] \| None` | `None` | パスの**正規表現**許可リスト |
| `deny_paths` | `Iterable[str] \| None` | `None` | パスの**正規表現**除外リスト |
| `deny_defaults` | `bool` | `True` | 既定の除外パターンを使う（管理画面・ログイン・カートなどを除外） |
| `order` | `"bfs" \| "dfs" \| "random"` | `"bfs"` | 探索順 |
| `max_depth` | `int \| None` | `None` | 開始 URL からのリンク階層の上限（None = 無制限） |
| `per_page_timeout_s` | `float` | `30.0` | 1 ページあたりの上限（秒） |
| `handle_modal_each_page` | `str \| None` | `None` | 各ページで割り込み画面に対処する agent ゴール |
| `persist_state` | `bool \| str` | `True` | 進捗を parent job に永続化（**attempt 跨ぎで再開可能**）。`str` を渡すと保存キー名になる |
| `host_dedup` | `bool` | `False` | **ホストを跨いだ既訪 URL の重複除去**（同じ URL を別ホストで再訪しない） |
| `recrawl_patterns` | `Iterable[str] \| None` | `None` | host_dedup 有効時に**再訪を許可**する URL glob パターン（カテゴリ一覧などに使う） |

## Visit のフィールド

`walk()` が yield するのは **`Visit`** インスタンス。**ページの読み込みに成功**したときだけ yield されます（失敗・オフドメイン・フィルタ除外は無音でスキップ）。

| フィールド | 型 | 説明 |
|---|---|---|
| `visit.n` | `int` | 1 始まりの**成功**訪問番号 |
| `visit.target` | `int` | walker に渡した `target_pages`（進捗表示用） |
| `visit.url` | `str` | **実際**今いる URL（リダイレクト後） |
| `visit.requested_url` | `str` | walker が `page.goto()` に渡した URL |
| `visit.depth` | `int` | 開始 URL からのリンク階層（start_url は 0） |
| `visit.outline` | `str` | ページのアウトライン（walker が事前取得済み・再フェッチ不要） |
| `visit.page` | `Page` | そのページに居る `Page` ハンドル（ループ外から閉包しなくて良い） |

進捗を表示する典型:

```python
async for visit in walk(page, target_pages=200):
    print(f"[{visit.n}/{visit.target}] depth={visit.depth} {visit.url}")
```

## 巡回範囲の絞り込み

### 同じドメインだけ

```python
walk(page, target_pages=200, same_domain=True)
```

### 複数ドメインを許可

```python
walk(
    page,
    target_pages=500,
    allowed_domains=["example.com", "cdn.example.com"],
)
```

### パスを許可・除外（正規表現）

```python
walk(
    page,
    target_pages=300,
    allow_paths=[r"^/articles/"],
    deny_paths=[r"^/admin/", r"\.pdf$"],
)
```

> `deny_defaults=True`（既定）のときは、管理画面・ログイン・カート・印刷ビューなどの**経験則的な除外**もあわせて適用されます。

### 階層・順序

```python
walk(page, target_pages=300, max_depth=2, order="dfs")
```

## 失敗の扱い

`walk()` は**成功した訪問だけ yield** します。失敗ページ・オフドメイン redirect・フィルタ除外は walker が無音でスキップするので、**ループ内で握りつぶす必要はありません**。

各ページ内の処理が落ちた場合だけ try で囲ってください:

```python
async for visit in walk(page, target_pages=300):
    try:
        await page.save_assets("out/images")
    except Exception as e:
        print(f"  skip {visit.url}: {e}")
```

## attempt 跨ぎで再開

`persist_state=True`（既定）のとき、walker の進捗（`crawled` / `queue`）は parent job のディスクに保存されます。途中で止まっても、**同じ `parent_job_id` で再開すれば続きから**走ります。

```python
async with cli.session("https://example.com", parent_job_id="crawl-001") as page:
    async for visit in walk(page, target_pages=10000, persist_state=True):
        ...
```

## クラス版 `Walker`

`walk()` は内部で `Walker` を作っています。ステートを覗きたい / 別の場所で組み立てたい場合に使います。

```python
from paprika_client import Walker

walker = Walker(page, target_pages=200, same_domain=True)
async for visit in walker:
    ...
print(len(walker.crawled), "pages crawled")
print(len(walker.queue), "still in queue")
```

## ベストプラクティス

- **`target_pages` を明示する** — 無限に走らせないための基本。
- **`max_depth` で浅く保つ** — フッタの外部リンクから無関係なドメインに迷い込まないように。
- **ループ内のサンプル数は `visit.n` を使う** — 自分で `i++` カウントしない（dedup でスキップされるとずれる）。
- **`requested_url` と `url` の食い違い**に注意 — 強制リダイレクトされたページを記録するなら `visit.url` を、リンク元 URL を残したいなら `visit.requested_url` を。
- **長時間クロールは `persist_state=True`** — 失敗時に最初からやり直さずに済みます。

## 関連

- [API リファレンス: walk](api.html#walk)
- [ガイド: サイトを巡回（walk）](guides.html)
- [ユースケース: サイト全体をアーカイブ](usecases.html)

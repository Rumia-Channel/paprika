---
layout: doc
title: walk — サイト巡回(BFS)
description: Paprika の walk() / Walker でサイトを BFS で巡回するときの全オプション（target_pages / same_domain / allowed_domains / max_depth / host_dedup）と Visit、進捗の追い方を解説。
active: walk
---

`walk()` はサイトを **BFS（幅優先）** で巡回しながら 1 ページずつ `Visit` を yield する**非同期イテレータ**です。dedup・深さ制御・dead-end フィルタを内部で処理するので、自前の BFS ループを書かなくても安定して巡回できます。

> 全体像は [はじめに](intro.html) / [ガイド](guides.html#walk) / 目的別の例は [ユースケース](usecases.html)。

## 最小例

```python
import asyncio
from paprika_client import paprika, walk

async def main():
    async with paprika() as cli:
        async with cli.session("https://example.com") as page:
            async for visit in walk(page, target_pages=50, same_domain=True):
                await visit.capture()       # その時点の HTML + 画像を保存
                print(f"[{visit.index}/{visit.target}] depth={visit.depth} {visit.url}")

asyncio.run(main())
```

- `walk(page, **opts)` は `Visit` を yield する **AsyncIterator**。クラス版は `Walker`（ステートを残しておきたいとき）。
- ループ中で `visit.capture()` を呼べば、そのページのアセットがジョブの結果に積まれます。

## オプション一覧

| 引数 | 既定 | 意味 |
|---|---|---|
| `target_pages` | `100` | 訪問ページ数の上限（達したら停止） |
| `same_domain` | `True` | 開始 URL と**同じドメイン**だけを巡回 |
| `allowed_domains` | `None` | 明示的に許可するドメインのリスト（指定すると `same_domain` より優先） |
| `max_depth` | `None` | 開始 URL からのリンク階層の上限（None = 無制限） |
| `host_dedup` | `False` | **ホストを跨いだ既訪 URL の重複除去**（同じ URL を別ホストで再訪しない） |

> `host_dedup` は普段オフ。複数の入口（CDN ホスト等）から同じコンテンツを集めるときは ON。

## Visit のフィールド

```python
async for visit in walk(page, target_pages=50):
    visit.url        # 今このページの URL
    visit.index      # 1 始まりの訪問番号
    visit.target     # walker に渡した target_pages（進捗表示に便利）
    visit.depth      # 開始 URL からのリンク階層
    visit.referrer   # どのページから辿られて来たか（最初は None）
    await visit.capture()                     # HTML + 画像を保存
    await visit.capture("custom-name")        # 名前を付けて保存
    visit.links                                # このページから抽出されたリンク一覧
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

### 階層を制限

```python
walk(page, target_pages=300, max_depth=2)   # トップから 2 リンク先まで
```

## dedup（重複除去）

- 既定で **既訪 URL は再訪しません**（同一ホスト内）。
- 別ホスト由来でも内容が重複する場合は **`host_dedup=True`** を指定すると、ホストを跨いで dedup します。

## 進捗を見る

```python
async for visit in walk(page, target_pages=200):
    print(f"[{visit.index}/{visit.target}] depth={visit.depth} {visit.url}")
```

管理画面の **Live パネル**でもリアルタイムに見られます。

## 失敗ページを許容して進める

途中で 1 ページの取得に失敗しても walker は止まりません（dead-end はスキップ）。**`visit.capture()` を try/except** で包めば、自分のループ内で失敗時の挙動を制御できます。

```python
async for visit in walk(page, target_pages=300):
    try:
        await visit.capture()
    except Exception as e:
        print(f"  skip {visit.url}: {e}")
```

## クラス版 `Walker`

`walk()` は内部で `Walker` を作っています。**外側で複数の `walk()` を組み合わせたい**ときは `Walker` を直接使ってください。

```python
from paprika_client import Walker

walker = Walker(page, target_pages=200, same_domain=True)
async for visit in walker:
    ...
```

## ベストプラクティス

- **`target_pages` を明示する** — 無限に走らせないための基本。
- **同じ URL は 2 回開かない** — `walk()` が dedup しているので、**自分で `i++` カウントしない**。サンプル数を正確に数えたいときは `visit.index` を使う。
- **`max_depth` で浅く保つ** — フッタの外部リンクから無関係なドメインに迷い込まないように。
- **失敗を握りつぶさない** — `visit.capture()` を try で包んで、`print(visit.url, exception)` を残しておくと後の調査が早い。

## 関連

- [API リファレンス: walk](api.html#walk)
- [ガイド: サイトを巡回（walk）](guides.html#walk)
- [ユースケース: サイト全体をアーカイブ](usecases.html)

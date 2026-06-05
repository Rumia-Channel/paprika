---
layout: doc
title: Locator — 要素の指定
description: Paprika の Locator(Playwright スタイル)で DOM 要素を指定するレシピ集 — CSS, テキスト, ロール, testid, placeholder, title, alt, インデックス, all, count, クリックや待機の実例。
active: locator
---

`Locator` は **要素の指し方**だけを保持して、`click()` や `wait_for()` のタイミングで初めて DOM を探しに行く遅延評価の参照です。Playwright と同じ感覚で書けます。

> 全 API は [リファレンス: Locator](api.html#locator)。複合的な使い方は [サンプル](examples.html) も参照。

## 作り方（早見表）

| メソッド | 例 | マッチするもの |
|---|---|---|
| `page.locator(css)` | `page.locator("button.primary")` | 任意の **CSS セレクタ** |
| `page.get_by_role(role, name=)` | `page.get_by_role("button", name="購入")` | `role=` 属性で書かれた要素 |
| `page.get_by_text(text)` | `page.get_by_text("ログイン")` | **テキストで一致**する要素 |
| `page.get_by_test_id(id)` | `page.get_by_test_id("submit-btn")` | `data-testid="..."` 属性 |
| `page.get_by_placeholder(text)` | `page.get_by_placeholder("メール")` | `placeholder="..."` |
| `page.get_by_title(text)` | `page.get_by_title("ヘルプ")` | `title="..."` |
| `page.get_by_alt(text)` | `page.get_by_alt("ロゴ")` | `<img alt="...">` |

## 基本

```python
btn = page.locator("button.primary")
await btn.click()
await btn.wait_for(state="visible", timeout=10)
```

## テキストで指す

```python
await page.get_by_text("カートに入れる").click()
```

## ロールと名前で指す（アクセシブルな書き方）

```python
await page.get_by_role("button", name="送信").click()
await page.get_by_role("link",   name="次へ").click()
```

## testid / placeholder / alt

```python
await page.get_by_test_id("login-form").wait_for()
await page.get_by_placeholder("メールアドレス").fill("alice@example.com")
await page.get_by_alt("メイン画像").wait_for()
```

## 複数ヒットの扱い

```python
items = page.locator("ul.products > li")
n = await items.count()
print(n, "items")

# インデックス指定
first  = items.nth(0)
last   = items.nth(-1)

# 全部取り出して処理
for it in await items.all():
    title = await it.locator(".title").text()
    print(title)
```

## 待機（タイミング制御）

```python
# 表示されるまで待つ（既定 5 秒）
await page.locator(".loading").wait_for(state="hidden", timeout=10)

# 出現を待ってからクリック
btn = page.get_by_role("button", name="続行")
await btn.wait_for(state="visible")
await btn.click()
```

## 入力と読み取り

```python
inp = page.locator("input[name='q']")
await inp.fill("paprika")
await inp.press("Enter")

el = page.locator(".item-name").nth(0)
print(await el.text())
print(await el.get_attribute("data-id"))
```

## 連鎖（子要素を絞り込む）

```python
card = page.locator(".card").nth(0)
title = card.locator("h2.title")
await title.click()
```

## 「見えない要素」を待たない

```python
# state は visible / hidden / attached / detached
await page.locator(".modal").wait_for(state="hidden")
```

## よくあるハマりどころ

- **複数ヒットでは `nth()` か `all()`** を使う（クリックは 1 つに絞る）。
- **`get_by_text` は部分一致**。完全一致したいときは CSS（`text-content` の制限あり）か属性で指す。
- **動的に出る要素**は `wait_for(state="visible")` で待ってから操作する。
- **CSS が効かない画面**（Canvas、Shadow DOM の深い所など）は [Vision AI（`page.agent`）](vision-mouse.html) に逃がす。

## 関連

- [API リファレンス: Locator](api.html#locator)
- [ガイド: DOM 操作](guides.html)
- [サンプル: クリック・入力・キー操作](examples.html)

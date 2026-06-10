# Paprika Bridge

Chrome extension that connects the operator's browser to a Paprika
Hub. It registers the browser's login cookies into the hub's per-host
registry so the worker fleet can reuse that login state on jobs.

Two modes, switched with the **開発モード (Developer mode)** toggle in
the popup's ⚙ menu:

* **自動モード (default, dev mode OFF)** — "set and forget". A
  background service worker watches the tab you're on and, a moment
  after you log into a site, pushes that host's cookies to
  `/hosts/{host}` automatically. The next job that targets the host
  gets your login state with no manual step. Only the host of the tab
  you're actively looking at is ever pushed.
* **開発モード (dev mode ON)** — auto-sync stops; registration becomes
  fully manual. You open a **table editor** (`editor.html`), visualise
  and edit the host's cookies in a spreadsheet (no JSON text
  wrangling), then click **Paprika に登録** to PUT them.

Jobs reference the resulting login state via
`options.cookies_from="example.com"`.

## Files

| File | Role |
|------|------|
| `manifest.json` | MV3 manifest (declares the background service worker) |
| `lib.js` | Shared helpers (`PB.*`): host/cookie normalisation, the chrome→paprika cookie projection, storage, and the push primitives. Loaded by the popup, the editor, AND `importScripts`'d by the service worker. |
| `background.js` | Auto-sync service worker (default mode) |
| `popup.html` / `popup.js` | Toolbar popup: ⚙ settings (hub URL + toggles), mode card, one mode-appropriate action |
| `editor.html` / `editor.js` | Full-page table cookie editor (developer mode) |

## Auto mode, exactly

Triggers (debounced ~2.5s, active-tab host only):

* a tab finishes loading (covers the post-login redirect)
* a cookie for the active host changes (covers XHR / SPA logins)

Each push:

1. collects the cookies the browser would send to the active page
   (`chrome.cookies.getAll({url})` — includes parent-domain cookies
   like `.example.com`),
2. skips the PUT if the host's cookie jar is unchanged since last push
   (fingerprint dedup),
3. `GET /hosts/{host}` to preserve any operator-set notes /
   popup_policy / recrawl_patterns, then `PUT` the new cookie list.

**登録済みホストのみ自動更新** (⚙): when ON, auto mode only refreshes
hosts already registered on the hub — casual browsing of new sites
won't create records. (Use the popup's "今すぐ登録" or the editor to add
a new host.)

## Dev mode — the table editor

Columns: ✓(include), name, value, domain, path, expires (epoch + a
human-readable hint; blank = session cookie), Secure, HttpOnly,
SameSite, delete.

Load rows from either source, edit, then register:

* **Chrome から読込** — the host's live cookies from this browser.
* **Hub から読込** — whatever is already registered on the hub.

A read-only "登録される JSON を確認" panel shows exactly what will be
PUT. Registration preserves operator-set host metadata, same as auto
mode.

Open it from the popup (dev mode → "このサイトの cookie を表で編集"), or
directly at `chrome-extension://<id>/editor.html?host=example.com`.

## What it does NOT cover

* `Login Data` (saved passwords) — Chrome's password manager API is
  not exposed to extensions.
* `Local Storage` / `Session Storage` / `IndexedDB` — accessible only
  via content scripts in pages the user actively visits, one origin at
  a time. Not worth the complexity here.

Cookies cover login state for >90% of sites. If you genuinely need a
full Chrome profile (autofill, saved logins, per-origin storage), use
`paprika-client upload-profile` from the CLI instead.

## Install

Two ways:

1. **From the hub's install page**:
   - Open `http://<your-hub>/profiles/extension/install` in Chrome
   - Download the .zip, extract
   - chrome://extensions → enable "Developer mode" → "Load unpacked"
     → pick the extracted folder
2. **Direct from the git source tree**:
   - chrome://extensions → "Load unpacked"
     → pick `server/web/extensions/paprika-bridge/`

Re-loading after a `git pull` is "click the refresh icon next to the
extension in chrome://extensions".

## Use

1. Click the toolbar icon (pin it from the puzzle-piece menu first).
2. Open ⚙ and enter the Hub URL (e.g. `http://paprika.lan`). Saved
   across popup invocations.
3. Leave dev mode OFF for hands-free auto-sync, or turn it ON to edit
   cookies in the table before registering.

Last write wins on the hub side, so repeat pushes are always safe.

## Permissions explained

| Permission | Why |
|------------|-----|
| `cookies` | Read cookies via `chrome.cookies.getAll()` + watch `onChanged` for auto-sync |
| `storage` | Remember the Hub URL + mode toggles + per-host last-sync time |
| `activeTab` + `tabs` | Read the current tab's URL to derive the host; detect page-load to trigger auto-sync |
| `<all_urls>` | Required by `chrome.cookies`; the extension never opens pages or injects scripts |

The background service worker only acts when a Hub URL is configured
and dev mode is OFF; it pushes only the active tab's host.

## Build / package for distribution

The hub serves a fresh .zip from
`GET /profiles/extension/paprika-bridge.zip`, built on demand by
zipping this directory (every file under it is included — new files
like `lib.js` / `background.js` / `editor.*` are picked up
automatically). Nothing to "build" — the source files load directly.

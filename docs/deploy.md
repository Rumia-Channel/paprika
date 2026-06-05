# Deploy — paprika 本番(マルチハブ)反映手順

> **要点: 本番ハブ (10.10.50.35/.36/.37) やワーカーを直接編集/scp/`docker restart`
> しない。コード変更は `.34`(SoT/制御ホスト)の `/opt/paprika/{server,core}` を
> 編集するだけ。あとは自動で全ハブ＋フリートに反映される。**

このドキュメントは git 管理(=全クローンに propagate)。同内容の「絶対ルール」要約が
ローカルの `CLAUDE.md`(gitignore・この機のみ)冒頭にもある。背景・診断レシピは
auto-memory `deploy-via-dot34` 参照。

---

## なぜ `.34` 一本化なのか

本番 `/opt/paprika` は **git ではない**(素のディレクトリ、`.git`無し)。以前は
複数の Claude セッション＋operator が **3ハブを個別に直編集**していた結果、
版ハッシュが数分でサウトゥース (`e38797→7398b0→f50763bf`…) し、ワーカーが
接続先ハブ次第で別バージョンへ収束する **two-writers 事故が頻発した (2026-06-05)**。

`.34` は nginx front / redis / reconciler を載せた制御ホスト(ハブではない)で、
**全ハブ＋全ワーカーへ鍵あり SSH** がある。ここを唯一の真実源(SoT)にして、
原子的に配ることで two-writers を根絶する。

旧 `scripts/deploy.sh`(単一ハブ・`git pull` 前提)は prod が git 化されていない
ため **動かない**。

---

## 手順 A — 自動(既定)

**`.34:/opt/paprika/{server,core}` を編集して保存。それだけ。**

systemd の `paprika-deploy-watcher`(**LIVE 2026-06-05**)が ~30秒後(debounce)に
変更を検知し、`scripts/deploy-from-34.sh` を実行して 3ハブ＋フリートへ反映する。

```bash
# 監視
ssh 10.10.50.34 'journalctl -u paprika-deploy-watcher -f'
# 一時停止して手動運用へ(再開は start)
ssh 10.10.50.34 'systemctl stop paprika-deploy-watcher'
```

## 手順 B — 手動(watcher を待たず即時)

```bash
ssh 10.10.50.34 'DRY_RUN=1 bash /opt/paprika/scripts/deploy-from-34.sh'   # 事前確認
ssh 10.10.50.34 'bash /opt/paprika/scripts/deploy-from-34.sh'             # 実行
```

フラグ(env): `DRY_RUN` `SKIP_WORKERS` `SKIP_HUBS` `FORCE_HUBS` `FORCE_REBALANCE`

---

## `deploy-from-34.sh` がやること

1. **分類(内容ハッシュ)** — `.34` と基準ハブを比較(rsync の itemize parse は不安定
   なので使わない):
   - `worker_hash` = `server/hub/**` と `scheduler.py` を除く全 `.py`
     (= `server/hub/_version.py` のワーカーハッシュ)。変われば**ワーカー自己更新**。
   - `hub_hash` = `server/worker/**` を除く全 `.py`(ハブプロセスが実行するコード)。
     変われば**ハブ再起動**が必要。
2. **原子的 rsync** `.34 -> 3ハブ`(`--checksum --delete`、並列)。
   **全ファイル着地後に再起動**(これでサウトゥース回避)。
3. **ローリングハブ再起動**(hub コード変更時のみ)。**`/health==200` ゲート**付きで、
   復帰しないハブがあれば **ABORT**(他ハブ無傷で停止。サイレント放置しない)。
4. **ローリングワーカー再起動**(バッチ)で収束＋ハブ間リバランス。
5. **収束 verify**(全ハブが同一版を配信・eligible 充足・submit probe)。

反映対象は **`server/` + `core/` のみ**。`scripts/`/`deploy/`/`.env`/`nginx.conf` は
対象外 = **デプロイツール自体は手動更新**(scp し直す)で、デプロイに巻き込まれない。

---

## gotcha(ハマりどころ)

- **ハブ `docker restart` は uvicorn の graceful-shutdown でハングする。** ワーカー WS
  等の長命接続が閉じるのを待って `"Waiting for connections to close"` で固まり、
  ハブが DOWN(health 000・ワーカー0)になることがある(2026-06-05 に hub-35 で実発生)。
  → スクリプトは **`-t 8`(即 SIGKILL)+ `/health` ゲート + ABORT** で対処。
  手動復旧: `ssh root@10.10.50.<ip> 'docker restart -t 8 hub-hub-a-1'` → `curl
  localhost:8100/health` が 200 を確認。
- **ハブ再起動はワーカーを変位させる → 空ハブ → 偽 503**("fleet at capacity" だが
  実際は空きあり)。手順4のローリングワーカー再起動が consistent-hash で均等に再配置
  して解消する。
- **two-writers**: ハブ直編集 ＋ 別の deploy が同時に走ると無限ループ。**必ず `.34`
  一本化**。watcher 稼働中はハブ直編集しないこと(`.34` 編集だけ)。

---

## 関連

- `scripts/deploy-from-34.sh` — 本体
- `scripts/deploy-watcher.sh` + `deploy/paprika-deploy-watcher.service` — 自動反映
- commits: `6a873aa`(tooling), `c17d3bd`(堅牢化)
- auto-memory: `deploy-via-dot34`, `multihub-nginx-routing-and-deploy`,
  `worker-fleet-ops`, `zero-downtime-worker-update`

# UmamusumeFactorDB

ウマ娘の「継承因子画面」スクリーンショットから、本人＋継承元2体の因子情報（青/赤/緑/白）と★数を抽出し、Google スプレッドシートに蓄積するツール。

## スコープ（MVP）

- 入力：スクロール結合済みの縦長画像（PNG）1 枚
- 出力：指定スプレッドシートの `factors_raw` タブに 1 行追記
- 投稿者 ID は CLI 引数で渡す

Google Form 連携・検索 UI は後続フェーズ。

## セットアップ

```bash
cd UmamusumeFactorDB
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash)
pip install -r requirements.txt
```

Google Sheets への書き込みは **Apps Script Web App**（Webhook）経由で行う。GCP プロジェクトは不要。

### Apps Script デプロイ手順

#### 初回セットアップ

1. 対象スプレッドシートを開く → 拡張機能 → Apps Script でプロジェクトを作成し `scriptId` を控える
2. プロジェクトの設定 → スクリプトプロパティ → `SHARED_SECRET` / `CLOUD_RUN_URL` / `CLOUD_RUN_SECRET` を登録
3. 一度手動でデプロイ（種類：ウェブアプリ、実行：自分、アクセス：全員）して `deploymentId` と Web App URL を取得
4. [config/apps_script_webhook.example.json](./config/apps_script_webhook.example.json) を `config/apps_script_webhook.json` にコピーし、`webhook_url` と `secret` を記入
5. スプレッドシートを一度開き直して、追加メニュー `UMG因子DB → ⏰ 1 時間ごとの自動反映トリガを設置` をクリック（バグ報告の自動反映を有効化）

#### 以後のコード更新（clasp 経由）

```bash
npm install -g @google/clasp   # 初回のみ
clasp login                    # 初回のみ

cd apps_script
# apps_script/.clasp.json の scriptId を自プロジェクトに合わせる
clasp push -f
clasp deploy -i <DEPLOYMENT_ID> -d "v<n>: 変更点の説明"
```

`-i <DEPLOYMENT_ID>` に既存の deployment を指定することで **Web App URL を維持** したまま新バージョンに切り替えられる。

## 使い方

```bash
python run.py path/to/image.png --submitter <投稿者ID>
python run.py path/to/image.png --submitter test --dry-run            # スプレに書かず JSON 出力
python run.py path/to/image.png --submitter test --debug-crops ./crops  # 切り出し保存
```

## Google Form / Cloud Run 自動化

投稿者が Google Form から画像を上げると、Cloud Run で自動処理してスプレに 3 行追加される構成：

- Form 作成 → [docs/form_setup.md](docs/form_setup.md)
- Cloud Run デプロイ → [docs/cloud_run_deploy.md](docs/cloud_run_deploy.md)

Cloud Run 上の処理サーバは [server/main.py](server/main.py)（FastAPI）、コンテナ定義は [server/Dockerfile](server/Dockerfile)。

## 参考

- [umasagashi/umacapture](https://github.com/umasagashi/umacapture)（MIT License, © 2022 umasagashi）— 座標定義 `recognizer.json` と ONNX モデルを参考にしている
- [ウマ娘DB](https://uma.pure-db.com/ja-jp/search) — 最終的に目指す検索サイトの形

## ライセンス表記

本プロジェクトは umacapture の設定ファイルを参考・一部流用しており、その部分には次の表記が含まれます：

```
Copyright (c) 2022 umasagashi
Released under the MIT License
```

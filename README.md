# UmamusumeFactorDB（UMG因子保管庫）

ウマ娘の「継承因子画面」スクリーンショットから、本人＋継承元 2 体の因子情報（青/赤/緑/白）と★数を AI が自動抽出し、Google スプレッドシートに蓄積・検索できる Web アプリケーションシステム。

## システム構成

```
投稿者 Form → Apps Script → Cloud Run (FastAPI + ONNX + EasyOCR)
                  ↓
         Google Sheets (factors_normalized)
                  ↓
         検索 UI（GitHub Pages） / Discord 通知
```

- **投稿**: Google Form 経由で因子画像を投稿（トレーナーID、連絡先、目的、用途、脚質、コメント）
- **自動解析**: Cloud Run 上で ONNX + EasyOCR により青/赤/緑/白因子と★数を抽出
- **蓄積**: スプレッドシート `factors_normalized` に 1 投稿あたり 3 行（main / parent1 / parent2）
- **検索**: GitHub Pages の検索 UI から条件指定で絞り込み閲覧
- **通知**: Discord Webhook で投稿内容を配信

詳細なアーキテクチャは [docs/system_spec.md](docs/system_spec.md)、構成図 pptx は [docs/system_architecture.pptx](docs/system_architecture.pptx)。

## 認識精度（2026-04-23 時点）

37 枚のゴールデンセット評価:

| 指標 | 値 |
|---|---|
| ★数一致率 | **35/37 (94.6%)** |
| 因子名誤認 | **5 件** |
| 悪化 | **0 件**（ベースラインの正解はすべて維持） |

残 7 件（★ 2 / 名前 5）の内訳と今後の方針は [docs/remaining_issues.md](docs/remaining_issues.md) を参照。

## セットアップ（ローカル開発）

```bash
cd UmamusumeFactorDB
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash)
pip install -r requirements.txt
```

## 使い方

### CLI で画像を解析

```bash
python run.py path/to/image.png --submitter <投稿者ID>
python run.py path/to/image.png --submitter test --dry-run            # スプレに書かず JSON 出力
python run.py path/to/image.png --submitter test --debug-crops ./crops  # 切り出し保存
```

### バッチ再評価（ゴールデンセット）

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/batch_recognize.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/evaluate_labels.py \
  --labels tests/fixtures/labels_2026-04-20T18-54-21.csv \
  --after  tests/fixtures/colored_factors/recognition_results.json
```

### 診断スクリプト

| スクリプト | 用途 |
|---|---|
| `scripts/diagnose_name_errors.py` | 因子名誤認の色別内訳をダンプ |
| `scripts/diagnose_star_errors.py` | ★誤認の HSV / CNN 内部状態を可視化 |
| `scripts/diagnose_red_candidates.py` | 赤因子の ONNX/OCR 候補スコアを並べる |
| `scripts/diagnose_green_fragments.py` | 緑因子の OCR 断片と辞書マッチ結果を並べる |
| `scripts/dump_red_crops.py` | 赤/青誤認ケースの crop を目視用に保存 |
| `scripts/dump_green_crops.py` | 緑誤認ケースの uma 内全緑 box を保存 |
| `scripts/dump_all_boxes.py` | 色問わず全 boxes の位置と HSV スコアをダンプ |

## Google Form / Cloud Run 自動化

投稿者が Google Form から画像を上げると Cloud Run で自動処理してスプレに 3 行追加される構成。

- Form 作成 → [docs/form_setup.md](docs/form_setup.md)
- Cloud Run デプロイ → [docs/cloud_run_deploy.md](docs/cloud_run_deploy.md)

Cloud Run 上の処理サーバは [server/main.py](server/main.py)（FastAPI）、コンテナ定義は [server/Dockerfile](server/Dockerfile)。

## Apps Script 連携

Google Sheets への書き込みは **Apps Script Web App**（Webhook）経由で行う。GCP プロジェクトは不要。

### 初回セットアップ

1. 対象スプレッドシートを開く → 拡張機能 → Apps Script でプロジェクトを作成し `scriptId` を控える
2. プロジェクトの設定 → スクリプトプロパティに以下を登録:
   - `SHARED_SECRET` / `CLOUD_RUN_URL` / `CLOUD_RUN_SECRET`
   - `DISCORD_WEBHOOK_URL_*`（チャンネル別 Webhook、複数可）
   - `SEARCH_UI_URL`（検索 UI の公開 URL）
3. 一度手動でデプロイ（種類：ウェブアプリ、実行：自分、アクセス：全員）して `deploymentId` と Web App URL を取得
4. [config/apps_script_webhook.example.json](./config/apps_script_webhook.example.json) を `config/apps_script_webhook.json` にコピーし、`webhook_url` と `secret` を記入
5. スプレッドシートを一度開き直して、追加メニュー `UMG因子DB → ⏰ 1 時間ごとの自動反映トリガを設置` をクリック

### 以後のコード更新（clasp 経由）

```bash
npm install -g @google/clasp   # 初回のみ
clasp login                    # 初回のみ

cd apps_script
# apps_script/.clasp.json の scriptId を自プロジェクトに合わせる
clasp push -f
clasp deploy -i <DEPLOYMENT_ID> -d "v<n>: 変更点の説明"
```

`-i <DEPLOYMENT_ID>` に既存の deployment を指定することで **Web App URL を維持** したまま新バージョンに切り替えられる。

## 主要な解析ロジック

現在の認識パイプライン（[src/umafactor/](src/umafactor/) 配下）は以下の設計:

- **cropper.py** — 画像から 3 ウマ娘分のセクション検出 → ★検出駆動で因子ボックスを動的に切り出し。色判定は左端 chip 幅 15%/22% の dual-score で小さい/大きい緑アイコン両方を救済
- **infer.py** — ONNX 因子推論（近傍摂動アンサンブル、★分類器 CNN 28×28 2 クラス）
- **ocr.py** — EasyOCR 補完。赤/青は allowlist OCR でゴミ文字を抑制、緑は readtext 断片を並列マッチ + 長さ比補正
- **pipeline.py** — 緑 box 選択戦略（OCR top1 conf 最大 box を名前採用、近傍 gold>0 box から★採用）、row 0 位置絶対化、rank fallback ガード

実装の詳細は [docs/system_spec.md](docs/system_spec.md) のデータフロー節を参照。

## ドキュメント索引

| ドキュメント | 内容 |
|---|---|
| [docs/system_spec.md](docs/system_spec.md) | システム全体仕様とデータフロー |
| [docs/remaining_issues.md](docs/remaining_issues.md) | 残課題の内訳と次プラン候補 |
| [docs/form_setup.md](docs/form_setup.md) | Google Form 設定手順 |
| [docs/cloud_run_deploy.md](docs/cloud_run_deploy.md) | Cloud Run デプロイ手順 |
| [docs/system_architecture.pptx](docs/system_architecture.pptx) | 構成図（PowerPoint） |

## 参考

- [umasagashi/umacapture](https://github.com/umasagashi/umacapture)（MIT License, © 2022 umasagashi）— 座標定義 `recognizer.json` と因子 ONNX モデル（813 クラス分類）の提供元
- [ウマ娘DB](https://uma.pure-db.com/ja-jp/search) — 目指す検索サイトの形

## ライセンス表記

本プロジェクトは umacapture の設定ファイルを参考・一部流用しており、その部分には次の表記が含まれます：

```
Copyright (c) 2022 umasagashi
Released under the MIT License
```

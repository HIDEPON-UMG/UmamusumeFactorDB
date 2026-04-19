# UMG因子保管庫 システム仕様書

## 1. 概要

ウマ娘の継承因子画面スクリーンショットから、本人 + 継承元 2 体の因子情報を AI が自動抽出し、Google スプレッドシートに蓄積・検索できる Web アプリケーションシステム。

## 2. 主要機能

| 機能 | 説明 |
|---|---|
| **投稿** | Google Form 経由で因子画像を投稿 |
| **自動解析** | Cloud Run 上で ONNX + EasyOCR により青/赤/緑/白因子 ＋ ★数を抽出 |
| **データ蓄積** | スプレッドシート `factors_normalized` に 1 投稿 = 3 行（親/祖1/祖2）で記録 |
| **検索 UI** | Apps Script Web App の HTML で複数条件フィルタ検索 |
| **Discord 通知** | 目的・用途（対人/査定/競技場）別に Webhook でキタサンブラック口調メッセージ投稿（表示名は Webhook 設定「おしらせキタちゃん」） |

## 3. システム構成

### 3.1 コンポーネント一覧

| 層 | コンポーネント | 役割 |
|---|---|---|
| フロント | Google Form | 投稿エントリポイント（画像 + 目的・用途 + 連絡先） |
| フロント | Apps Script Web App (`search.html`) | 検索 UI |
| 制御 | Apps Script (`Code.gs`) | Form トリガ / Webhook / 検索 API / Discord 通知 |
| 処理 | Cloud Run (`factor-processor`) | FastAPI + ONNX + EasyOCR による画像解析 |
| データ | Google Sheets | `factors_normalized` (解析結果)、`フォームの回答 1` (応答原本) |
| データ | Google Drive | 投稿画像ファイル（匿名リネーム済み） |
| 通知 | Discord Webhook × 3 | 対人 / 査定 / 競技場 チャンネル別 |
| 外部 | GCP Secret Manager | `apps-script-secret`, `cloud-run-shared-secret` |

### 3.2 デプロイ先

| 項目 | 値 |
|---|---|
| GCP プロジェクト | `factor-ocr` |
| Cloud Run リージョン | `asia-northeast1` |
| Cloud Run サービス名 | `factor-processor` |
| Cloud Run エンドポイント | `https://factor-processor-341799293316.asia-northeast1.run.app/process` |
| Apps Script Web App | `https://script.google.com/macros/s/AKfyc.../exec` |
| 検索画面 | 上記 + `?ui=search` |

## 4. データフロー

### 4.1 投稿フロー

```
ユーザー
  │
  ▼ (1) 画像 + 連絡先 + 目的・用途を送信
Google Form
  │
  ▼ (2) 応答タブに自動記録
Google Sheet「フォームの回答 1」
  │
  ▼ (3) onFormSubmit トリガ発火
Apps Script (Code.gs)
  │ ├─ ① 画像ファイルを factor_yyyyMMdd_HHmmss に匿名リネーム
  │ └─ ② 画像 base64 + secret を POST
  ▼
Cloud Run /process (FastAPI)
  │ ├─ ③ 画像クロップ（cropper.py）
  │ ├─ ④ ONNX 因子推論（infer.py）
  │ ├─ ⑤ EasyOCR 補完（ocr.py）
  │ ├─ ⑥ 固有スキル → ウマ娘 逆引き（unique_skill_to_character.json）
  │ └─ ⑦ Apps Script webhook (doPost) に解析結果 POST
  ▼
Apps Script doPost
  │
  ▼ 3 行を書き込み
Google Sheet「factors_normalized」
  │
  ▼ (4) 応答タブに submission_id と status を書き戻す
  │
  ▼ (5) 目的・用途が対人/査定/競技場なら Webhook に投稿
        ・画像は Drive から Blob で取得
        ・multipart/form-data で Discord に直接添付（Drive 公開化はしない）
        ・embed.image は `attachment://<filename>` で参照
Discord Webhook（目的別チャンネル）
  │
  ▼ キタサン口調メッセージ + 添付画像 + 因子サマリ
Discord チャンネル（表示名は Webhook 設定の「おしらせキタちゃん」）
```

### 4.2 検索フロー

```
ユーザー
  │
  ▼ ブラウザで `.../exec?ui=search` を開く
Apps Script doGet
  │
  ▼ HtmlService で search.html を返す
ブラウザ（検索 UI）
  │
  ▼ 起動時 google.script.run.getFilterOptions()
Apps Script getFilterOptions
  │
  ▼ factors_normalized + Form 応答タブから選択肢収集
プルダウン選択肢を返す
  │
  ▼ ユーザーが条件を選択 → 検索ボタン
google.script.run.searchFactors(filters)
  │
  ▼
Apps Script searchFactors
  │ ├─ factors_normalized 全行読み込み
  │ ├─ submission_id で 3 行を集約 → submission オブジェクト
  │ ├─ 各条件を scope（全体/親のみ/祖のみ）単位で評価
  │ ├─ 画像 URL / 目的・用途を Form 応答タブから補完
  │ └─ 新しい順にソート
  ▼
検索結果を返す（submissions: [...]）
  │
  ▼ 結果テーブルを描画
ブラウザ
```

## 5. データスキーマ

### 5.1 `factors_normalized` タブ（解析結果）

1 投稿 = 3 行（main/parent1/parent2）で構成。共通列 + 因子列。

| 列 | 型 | 説明 |
|---|---|---|
| submission_id | string (UUID) | 投稿ごとに一意。3 行で共通 |
| submitted_at | string (ISO8601) | 投稿日時 |
| submitter_id | string | 連絡先（Discord 名など、任意） |
| image_filename | string | サーバ内仮名 |
| role | string | `main` / `parent1` / `parent2` |
| character | string | `[衣装名]キャラ名` 形式 |
| blue_type / blue_star | string / int | 青因子（例: スピード★2） |
| red_type / red_star | string / int | 赤因子（例: 中距離★2） |
| green_name / green_star | string / int | 緑因子（固有スキル） |
| factor_01_name / factor_01_star ～ factor_60_name / factor_60_star | string / int | 白因子スロット（最大 60 セット） |

### 5.2 `フォームの回答 1` タブ（応答原本）

Google Form が自動作成。列の例：
- タイムスタンプ
- 【任意】連絡先（Discord 名）
- 因子画像（Drive URL）
- 目的・用途
- submission_id（Apps Script が書き足し）
- status（同上）

### 5.3 外部参照データ（Cloud Run コンテナ内）

| ファイル | 件数 | 用途 |
|---|---|---|
| `config/recognizer.json` | - | 因子ボックス座標定義（umacapture 由来） |
| `config/unique_skill_to_character.json` | 249 | 固有スキル名 → `[衣装名]キャラ名` 逆引き |
| `models/modules/*/prediction.onnx` | 複数 | 因子/ランク/character の ONNX モデル |
| `models/modules/factor_info.json` | 813 | 因子マスタ（青/赤/緑/白タグ） |
| `/models/easyocr/` | - | EasyOCR 日本語+英語モデル（事前 DL） |

## 6. 主要な環境変数・シークレット

### Cloud Run

| 名前 | 種別 | 内容 |
|---|---|---|
| `APPS_SCRIPT_WEBHOOK_URL` | env | Apps Script の `exec` URL |
| `TARGET_TAB` | env | 書き込み先タブ名 `factors_normalized` |
| `APPS_SCRIPT_SECRET` | Secret Manager | Apps Script doPost 認証シークレット |
| `SHARED_SECRET` | Secret Manager | Cloud Run /process 認証シークレット |

### Apps Script スクリプトプロパティ

| 名前 | 内容 |
|---|---|
| `SHARED_SECRET` | doPost 認証（Cloud Run の `APPS_SCRIPT_SECRET` と同値） |
| `CLOUD_RUN_URL` | Cloud Run `/process` URL |
| `CLOUD_RUN_SECRET` | Cloud Run 認証（`SHARED_SECRET` と同値） |
| `FORM_RESPONSES_TAB` | 任意（未設定時は自動検出） |

## 7. セキュリティ対策

| 項目 | 対策 |
|---|---|
| 画像ファイル名の個人情報 | Apps Script 側で `factor_yyyyMMdd_HHmmss.ext` に自動匿名リネーム |
| clickjacking | `HtmlOutput` の `XFrameOptionsMode` をデフォルトに（ALLOWALL 解除） |
| webhook 認証 | Apps Script ↔ Cloud Run 間は `SHARED_SECRET` で認証 |
| シークレット管理 | Cloud Run は Secret Manager、Apps Script はスクリプトプロパティ |
| 共有範囲 | Discord 通知では画像を multipart で直接添付するため Drive の共有権限は変更しない（Form 既定のまま） |
| XSS | search.html の結果描画は全て `escapeHtml()` 経由 |

## 8. 主要ファイル構成

```
UmamusumeFactorDB/
├── apps_script/
│   ├── Code.gs              # webhook / onFormSubmit / 検索 API / Discord 通知
│   └── search.html          # 検索 UI（Apps Script Web App）
├── config/
│   ├── recognizer.json      # 因子ボックス座標（umacapture 由来）
│   ├── unique_skill_to_character.json  # 固有スキル → ウマ娘対応 249 件
│   └── apps_script_webhook.json        # webhook URL + secret（gitignore）
├── docs/
│   ├── form_setup.md        # Google Form 作成手順
│   ├── cloud_run_deploy.md  # Cloud Run デプロイ手順
│   ├── system_spec.md       # 本書
│   └── system_architecture.pptx  # 構成図
├── models/
│   └── modules/             # ONNX モデル + labels.json
├── scripts/
│   └── fetch_unique_skills.py  # UmaTools から 固有→カード対応を生成
├── server/
│   ├── main.py              # Cloud Run FastAPI
│   ├── requirements.txt
│   └── Dockerfile
├── src/umafactor/
│   ├── cropper.py           # 画像クロップ・ウマ娘セクション検出
│   ├── infer.py             # ONNX 推論
│   ├── ocr.py               # EasyOCR + rapidfuzz
│   ├── pipeline.py          # 統合パイプライン
│   ├── review.py / review_ui.py  # tkinter レビュー UI（CLI 用）
│   ├── schema.py            # スプレ行スキーマ（MAX 60 スロット）
│   └── config.py
├── Dockerfile               # ルート用（Cloud Run の --source . 対応）
├── .dockerignore
├── .gcloudignore            # ONNX を除外しないよう上書き
└── run.py                   # ローカル CLI 版
```

## 9. 運用上の注意

- **モデル更新**：固有スキル → ウマ娘対応表は `python scripts/fetch_unique_skills.py` で UmaTools リポジトリから再生成。新カード追加時に実行。
- **再デプロイ**：
  - Cloud Run 側コード変更 → `gcloud run deploy factor-processor --source .`
  - Apps Script コード変更 → エディタで貼り直し → 「デプロイを管理 → 新バージョン」
- **Discord 画像表示不可**：Drive Blob 取得に失敗、または画像サイズが Discord 制限（通常 8MB / ブースト時 50MB）を超えた場合に表示されない。Apps Script ログで `blob fetch failed:` や webhook status 4xx/413 を確認。
- **タイムアウト**：Cloud Run は 300 秒制限。コールドスタート約 30〜60 秒 + 解析 30〜60 秒の合計で収まる想定。

## 10. 参照

- [umasagashi/umacapture](https://github.com/umasagashi/umacapture) — 座標定義・ONNX モデル提供元（MIT License）
- [daftuyda/UmaTools](https://github.com/daftuyda/UmaTools) — 固有スキル → カード対応データソース
- [ウマ娘DB](https://uma.pure-db.com/ja-jp/search) — UI 参考

# UMG因子保管庫 システム仕様書

## 1. 概要

ウマ娘の継承因子画面スクリーンショットから、本人 + 継承元 2 体の因子情報を AI が自動抽出し、Google スプレッドシートに蓄積・検索できる Web アプリケーションシステム。

## 2. 主要機能

| 機能 | 説明 |
|---|---|
| **投稿** | Google Form 経由で因子画像を投稿（トレーナーID・連絡先・目的・用途・利用脚質・【任意】コメント を同時取得） |
| **自動解析** | Cloud Run 上で ONNX + EasyOCR により青/赤/緑/白因子 ＋ ★数を抽出 |
| **データ蓄積** | スプレッドシート `factors_normalized` に 1 投稿 = 3 行（親/祖1/祖2）で記録。投稿単位で `factor_no`（通番）を付与 |
| **検索 UI** | Apps Script Web App の HTML で複数条件フィルタ検索。目的タグを色分け表示（対人=赤/査定=青/競技場=緑）、利用脚質タグは淡色系（逃=淡赤/先=淡橙/差=淡青/追=淡紫/汎=淡緑）。コメントは部分一致検索可。青赤緑の条件セクションも色分け。モバイルはフィルタ行を 1 列・カード型レイアウトに切替 |
| **アーカイブ** | デフォルトは直近 30 日以内のみ表示。トグル ON で全期間を対象に拡張 |
| **バグ報告** | 検索画面から誤認識を報告 → `bug_reports` シートに蓄積 |
| **バグ自動反映ワーカ** | 時限トリガ（1 時間毎）または手動メニューで `bug_reports` を走査し、`factors_normalized` を自動補正 |
| **画像匿名化ツール** | スプレッドシートメニュー `UMG因子DB → 🙈 投稿画像ファイル名を一括匿名化` / `🔒 フォーム設定を安全化` で個人情報の残留を除去 |
| **Discord 再通知** | `UMG因子DB → 📣 Discord に再通知` で `factor_no` を指定して過去投稿を Webhook に再送 |
| **列ズレ修復** | `UMG因子DB → 🔧 列ズレ行を修復` で factor_no 導入直後の移行期に発生した 1 列シフトを補正 |
| **Discord 通知** | 目的・用途（対人/査定/競技場）別に Webhook でキタサンブラック口調メッセージ投稿（表示名は Webhook 設定「おしらせキタちゃん」） |

## 3. システム構成

### 3.1 コンポーネント一覧

| 層 | コンポーネント | 役割 |
|---|---|---|
| フロント | Google Form | 投稿エントリポイント（画像 + 目的・用途 + 連絡先） |
| フロント | Apps Script Web App (`search.html`) | 検索 UI |
| 制御 | Apps Script (`Code.gs`) | Form トリガ / Webhook / 検索 API / Discord 通知 |
| 処理 | Cloud Run (`factor-processor`) | FastAPI + ONNX + EasyOCR による画像解析 |
| データ | Google Sheets | `factors_normalized`（解析結果・`factor_no` 付）、`bug_reports`（誤認識報告）、`フォームの回答 1`（応答原本） |
| データ | Google Drive | 投稿画像ファイル（匿名リネーム済み） |
| 通知 | Discord Webhook × 3 | 対人 / 査定 / 競技場 チャンネル別 |
| 運用 | clasp（Google Apps Script CLI） | `apps_script/` 配下の差分 push と URL 保持デプロイを自動化 |
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
  │ ├─ payload の columns 名をシートヘッダーで引き、対応位置に書き込む
  │ │  （旧実装は常に A 列から書いていたため factor_no 列挿入後に 1 列ズレる
  │ │   バグがあった。列名マッピング方式に変更済み）
  │ └─ 未知列は末尾に追加してから書き込み
  ▼ 3 行を書き込み
Google Sheet「factors_normalized」
  │
  ▼ (4) 応答タブに submission_id と status を書き戻す
  │
  ▼ (5) 目的・用途が対人/査定/競技場なら Webhook に投稿
        ・画像は Drive から Blob で取得
        ・multipart/form-data で Discord に直接添付（Drive 公開化はしない）
        ・embed.image は `attachment://<filename>` で参照
        ・embed.description 先頭に OCR 誤認識の注意書き（blockquote）を挿入
        ・embed.fields に 📇 連絡先（表示のみ）→ 🆔 トレーナーID（コードブロック・コピー可）→ 投稿フォーム誘導 の順
Discord Webhook（目的別チャンネル）
  │
  ▼ キタサン口調メッセージ + 添付画像 + 因子サマリ + 連絡先 + トレーナーID
Discord チャンネル（表示名は Webhook 設定の「おしらせキタちゃん」）

── 再通知（後追い投稿）：メニュー `📣 Discord に再通知` →
   factor_no 指定でプロンプト入力 → resendDiscordByFactorNo が
   ① submission_id UUID 完全一致
   ② image_filename の `form-<sid 先頭 8 文字>` prefix で Form 応答に前方一致
   ③ submitted_at とタイムスタンプの近接（±10 分）
   の順で Form 応答レコードを特定し、通知されなかった投稿を遡って Webhook へ送信する。
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
  │ └─ _ensureFactorNo: factor_no 列の存在保証 + 未採番行にバックフィル
  ▼
  ▼ factors_normalized + Form 応答タブから選択肢収集
プルダウン選択肢を返す
  │
  ▼ ユーザーが条件を選択 → 検索ボタン（include_archive フラグを送信）
google.script.run.searchFactors(filters)
  │
  ▼
Apps Script searchFactors
  │ ├─ factors_normalized 全行読み込み
  │ ├─ submission_id で 3 行を集約 → submission オブジェクト（factor_no 付）
  │ ├─ include_archive=false の場合、submitted_at が「現在 - 30 日」より古い投稿を除外
  │ ├─ 各条件を scope（全体/親のみ/祖のみ）単位で評価
  │ ├─ 画像 URL / トレーナーID / 目的・用途 / 利用脚質 / コメント を Form 応答タブから補完
  │ └─ 新しい順にソート
  ▼
検索結果を返す（submissions: [...]）
  │
  ▼ 結果テーブルを描画（左端に #factor_no、目的タグは色分け）
ブラウザ
```

### 4.3 バグ報告 → 自動反映フロー

```
ユーザー（検索画面）
  │
  ▼ 🐛 バグ報告 or 結果左端の #factor_no クリック
バグ報告モーダル（因子No・対象ロール・項目・現在値・正しい値）
  │
  ▼ google.script.run.reportBug(params)
Apps Script reportBug
  │ └─ bug_reports シートに status="pending" で 1 行追記
  ▼
  ─── 時限トリガ（1 時間毎） or メニュー「🛠 バグ報告を適用」 ───
  ▼
Apps Script applyBugReports
  │ ├─ status="pending" の行を走査
  │ ├─ factor_no + target_role + wrong_value で該当行を特定
  │ ├─ 現在値が wrong_value と一致する場合のみ correct_value で上書き
  │ ├─ white_name は factor_NN_name スロットを検索して置換
  │ └─ 結果を status / applied_at / reviewer_note に書き戻し
  ▼
factors_normalized 更新 + bug_reports 状態更新
  │
  ▼ applied / invalid / needs_review / skipped の集計を返却
スプレッドシート（toast または alert 経由で運用者に通知）
```

**判定結果**

| status | 条件 |
|---|---|
| `applied` | 該当行が特定でき、現在値が `wrong_value` と一致、`correct_value` で上書き成功 |
| `invalid` | 該当 factor_no なし／現在値が `wrong_value` と不一致／数値変換失敗 など |
| `needs_review` | `white_star` / `other` や、`target_role` 未指定で一意に特定できないケース |

## 5. データスキーマ

### 5.1 `factors_normalized` タブ（解析結果）

1 投稿 = 3 行（main/parent1/parent2）で構成。共通列 + 因子列。

| 列 | 型 | 説明 |
|---|---|---|
| factor_no | int | 投稿単位の通番（同一 submission は同一番号）。`_ensureFactorNo()` が自動採番・バックフィル |
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

### 5.2 `bug_reports` タブ（誤認識報告）

検索画面のバグ報告モーダルが初回 POST 時に自動作成。自動反映ワーカの入力兼処理ログを兼ねる。

| 列 | 型 | 説明 |
|---|---|---|
| reported_at | string | 報告日時（Asia/Tokyo） |
| factor_no | int | 対象投稿の通番（必須） |
| target_role | string | `main` / `parent1` / `parent2` / 空 |
| wrong_field | string | `character` / `blue_type` / `blue_star` / `red_type` / `red_star` / `green_name` / `green_star` / `white_name` / `white_star` / `other` |
| wrong_value | string | 現在の誤った値（整合性チェック用） |
| correct_value | string | 正しい値 |
| status | string | `pending` / `applied` / `invalid` / `needs_review` |
| applied_at | string | ワーカが適用した日時（`applied` 時のみ） |
| reviewer_note | string | ワーカの処理結果または運用者メモ |

### 5.3 `フォームの回答 1` タブ（応答原本）

Google Form が自動作成。列の例：
- タイムスタンプ
- トレーナーID
- 【任意】連絡先（Discord 名）
- 因子画像（Drive URL）
- 目的・用途（対人 / 査定 / 競技場）
- 利用脚質（逃げ / 先行 / 差し / 追込 / 汎用）
- 【任意】コメント（30 字以内、検索 UI で部分一致検索可）
- submission_id（Apps Script が書き足し）
- status（同上）

### 5.4 外部参照データ（Cloud Run コンテナ内）

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
| 画像ファイル名の個人情報 | 3 重防御：(1) `onFormSubmit` で Drive ファイル名を `factor_yyyyMMdd_HHmmss.ext` に即時リネーム＋成功/失敗ログ、(2) Discord 送信直前に `imageBlob.setName()` で再度上書き、(3) 過去投稿を一括匿名化する `renameAllFormUploads()` をメニューから実行可能 |
| フォーム結果の露出 | `secureFormSettings()` で `setPublishingSummary(false)` / `setAllowResponseEdits(false)` を適用し、投稿者が他人のアップロード画像名・トレーナーID を閲覧不可にする |
| OAuth スコープ | `appsscript.json` に `drive` / `spreadsheets` / `forms` / `script.external_request` / `script.scriptapp` / `script.container.ui` を明示。最小権限推定での `drive.file` に落とされないようにし、`DriveApp.File.setName` を確実に許可 |
| clickjacking | `HtmlOutput` の `XFrameOptionsMode` をデフォルトに（ALLOWALL 解除） |
| webhook 認証 | Apps Script ↔ Cloud Run 間は `SHARED_SECRET` で認証 |
| シークレット管理 | Cloud Run は Secret Manager、Apps Script はスクリプトプロパティ |
| 共有範囲 | Discord 通知では画像を multipart で直接添付するため Drive の共有権限は変更しない（Form 既定のまま） |
| XSS | search.html の結果描画は全て `escapeHtml()` 経由 |

## 8. 主要ファイル構成

```
UmamusumeFactorDB/
├── apps_script/
│   ├── Code.gs              # webhook / onFormSubmit / 検索 API / Discord 通知 / バグ自動反映
│   ├── search.html          # 検索 UI（Apps Script Web App）
│   ├── appsscript.json      # GAS マニフェスト（タイムゾーン・Web App 設定）
│   └── .clasp.json          # clasp 用 scriptId（rootDir）
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
  - Apps Script コード変更 → `apps_script/` で `clasp push -f && clasp deploy -i <DEPLOYMENT_ID> -d "<説明>"`。既存 Deployment ID を再利用することで **Web App URL を維持** しつつ新バージョンを差し替えられる。
- **バグ報告自動反映の有効化（初回のみ）**：対象スプレッドシートを開き、メニュー `UMG因子DB → ⏰ 1 時間ごとの自動反映トリガを設置`。以後 `applyBugReports` が時限実行される。停止は `⛔ 自動反映トリガを削除`、即時適用は `🛠 バグ報告を適用（今すぐ）`、影響確認だけしたい場合は `🧪 ドライラン`。
- **過去画像の一括匿名化（初回のみ）**：既存の投稿画像が `combine_XXXX - トレーナー◯◯.png` のような投稿者名入りファイル名になっている場合、メニュー `UMG因子DB → 🙈 投稿画像ファイル名を一括匿名化` で応答シート全件を `factor_yyyyMMdd_HHmmss.ext` に置換可能。
- **フォーム結果の非公開化（初回のみ）**：メニュー `UMG因子DB → 🔒 フォーム設定を安全化` を 1 回クリックすると、投稿完了ページの「結果の概要を表示」リンクと回答編集リンクが恒久的に無効化される。
- **OAuth 再承認**：`appsscript.json` のスコープを変更した直後は GAS 側で再承認ダイアログが必要。任意の関数を Apps Script エディタから 1 回実行して「許可」すれば以後のトリガも自動で新スコープを使用する。
- **列ズレの事後修復（一度きりの移行用）**：`factor_no` 列を A 列に挿入した影響で、旧 `doPost` が書いた行は 1 列左にシフトしていた。メニュー `UMG因子DB → 🔧 列ズレ行を修復` を 1 回実行すると、B 列以降を右シフトして復旧する。失われた元 `submission_id` は `recovered-<factor_no>-<短縮 id>` で埋めるため、検索 UI 側の submission_id グルーピングは引き続き機能する。
- **Discord 通知の後追い送信**：`onFormSubmit` の通知をスキップしてしまった投稿や、Cloud Run/Apps Script 障害時の救済として `UMG因子DB → 📣 Discord に再通知` を使用する。プロンプトに factor_no をカンマ区切りで入力（例：`3,4,5`）。結果 alert には採用されたマッチング戦略（`uuid-exact` / `filename-prefix:xxxxxxxx` / `timestamp-near:<ms>`）も表示される。
- **OCR 誤認識の一時対応**：OCR 精度が安定するまで `KITASAN_OCR_DISCLAIMER` 定数を Discord embed.description の先頭に blockquote で挿入している。安定後は定数を空文字にするだけで非表示になる。
- **バグ反映が `needs_review` / `invalid` で止まる典型ケース**：
  - `white_star`・`other` は自動化対象外（`needs_review`）。`bug_reports` で内容を確認し、`factors_normalized` を手動編集してから `status` を `applied` に書き換える。
  - 現在値が `wrong_value` と既に違う場合は `invalid`。別のバグ報告や手動修正と重複している可能性が高いので、`reviewer_note` を見て判断。
- **Discord 画像表示不可**：Drive Blob 取得に失敗、または画像サイズが Discord 制限（通常 8MB / ブースト時 50MB）を超えた場合に表示されない。Apps Script ログで `blob fetch failed:` や webhook status 4xx/413 を確認。
- **タイムアウト**：Cloud Run は 300 秒制限。コールドスタート約 30〜60 秒 + 解析 30〜60 秒の合計で収まる想定。
- **検索 UI の既定レンジ**：アーカイブトグル OFF のとき投稿から 30 日以内のみ表示。古い因子を参照したい場合はトグル ON に切替（再検索が自動で走る）。
- **モバイルレイアウト**：フィルタ行は 640px 以下で 1 列縦並びに切り替え、検索結果テーブルは親/祖1/祖2 を色ラベル付きでカード型に変換する。画像サムネイルは PC で max-height 486px（0.9×）、モバイルで 288px に抑え、目的・用途タグと利用脚質タグの横並びと 💬 コメント表示のためのスペースを確保。

## 10. 参照

- [umasagashi/umacapture](https://github.com/umasagashi/umacapture) — 座標定義・ONNX モデル提供元（MIT License）
- [daftuyda/UmaTools](https://github.com/daftuyda/UmaTools) — 固有スキル → カード対応データソース
- [ウマ娘DB](https://uma.pure-db.com/ja-jp/search) — UI 参考

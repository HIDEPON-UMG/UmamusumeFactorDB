# Cloud Run デプロイ手順

Google Form 投稿を自動処理するため、Python 処理サーバ (FastAPI) を Google Cloud Run にデプロイする。

## 前提

- GCP プロジェクト `FACTOR-OCR` 作成済み
- スプレッドシートの Apps Script Web App デプロイ済み（factors webhook URL + SHARED_SECRET）
- ローカルに [Google Cloud SDK (gcloud)](https://cloud.google.com/sdk/docs/install) インストール済み
- Docker は **不要**（Cloud Run の Cloud Build を使えばソースから直接ビルド可能）

## 1. プロジェクトと API の有効化

```bash
gcloud auth login
gcloud config set project FACTOR-OCR

# 課金アカウントをリンク（無料枠利用でも必須）
gcloud billing projects link FACTOR-OCR --billing-account=<請求アカウントID>

# 必要 API を有効化
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com
```

> 請求アカウント ID は Cloud Console → お支払い → 「アカウント ID」で確認。

## 2. シークレットの登録（Secret Manager 推奨）

Cloud Run の環境変数として平文でも渡せるが、Secret Manager 経由が安全。

```bash
# Cloud Run → Apps Script webhook 書き込み用
echo -n "<apps_script_webhook.json の secret と同じ値>" | \
  gcloud secrets create apps-script-secret --data-file=- --project=FACTOR-OCR

# Apps Script → Cloud Run 認証用（新規にランダム文字列を作る）
echo -n "<十分ランダムな文字列>" | \
  gcloud secrets create cloud-run-shared-secret --data-file=- --project=FACTOR-OCR
```

## 3. Cloud Run へデプロイ

プロジェクトルート（`UmamusumeFactorDB/`）で以下を実行：

```bash
gcloud run deploy factor-processor \
  --source . \
  --region=asia-northeast1 \
  --platform=managed \
  --allow-unauthenticated \
  --memory=2Gi \
  --cpu=2 \
  --min-instances=0 \
  --max-instances=3 \
  --timeout=120s \
  --concurrency=1 \
  --set-env-vars="APPS_SCRIPT_WEBHOOK_URL=<https://script.google.com/macros/s/.../exec>,TARGET_TAB=factors_normalized" \
  --set-secrets="APPS_SCRIPT_SECRET=apps-script-secret:latest,SHARED_SECRET=cloud-run-shared-secret:latest"
```

オプションの補足：

- `--source .` で Cloud Build がリポジトリ直下の `Dockerfile` を自動検出（今回は `server/Dockerfile` なので `--dockerfile server/Dockerfile` でも可）
- Dockerfile は `datasets/red_blue_templates/`, `datasets/star_templates/`, `datasets/green_name_templates/` を必ずイメージに焼き込む（`templates.py` がランタイム参照するため、無いと Red 認識精度が大幅劣化）
- `--allow-unauthenticated` は Apps Script から公開エンドポイントとして呼ぶため必要（共有シークレットで認証）
- `--concurrency=1` は推論がメモリ/CPU を占有するので 1 リクエスト 1 インスタンスに
- `--memory=2Gi` は torch + EasyOCR + ONNX で必要
- `--timeout=120s` は 1 画像の処理時間に余裕を持たせる
- `--min-instances=0` で完全な無料枠運用（コールドスタートあり）

初回ビルドは Docker イメージが 2〜3 GB になるため 10〜20 分かかる。

## 4. Cloud Run URL を Apps Script に登録

デプロイ完了後にコマンド出力される URL（例：`https://factor-processor-xxxxx-an.a.run.app`）をメモ。

Apps Script エディタ → プロジェクトの設定 → スクリプトプロパティ：

| プロパティ | 値 |
|---|---|
| `CLOUD_RUN_URL` | `https://factor-processor-xxxxx-an.a.run.app/process` |
| `CLOUD_RUN_SECRET` | 手順 2 で設定した `cloud-run-shared-secret` の値と同じ |

## 5. ヘルスチェック

```bash
curl https://factor-processor-xxxxx-an.a.run.app/healthz
```

期待応答：`{"ok":true,"target_tab":"factors_normalized","has_webhook":true}`

## 6. ローカル画像でスモークテスト

```bash
# Base64 エンコードした画像を POST
.venv/Scripts/python.exe -c "
import base64, json, requests
img = base64.b64encode(open('tests/fixtures/sample_oguricap.png','rb').read()).decode()
r = requests.post('https://factor-processor-xxxxx-an.a.run.app/process',
    json={'secret': '<CLOUD_RUN_SECRET>', 'submitter_id': '@test', 'image_base64': img},
    timeout=120)
print(r.status_code, r.text[:500])
"
```

スプレッドシートの `factors_normalized` タブに 3 行追加されれば成功。

## トラブルシューティング

- **ビルドが OOM で失敗**：Cloud Build のマシンサイズを大きくする `--machine-type=e2-highcpu-8`
- **デプロイ後 503 / crash**：`gcloud run services logs read factor-processor --region=asia-northeast1`
- **タイムアウト**：1st gen は最大 60 分まで拡張可、但し通常は 120s で十分
- **認証エラー (401)**：`SHARED_SECRET` の値が Cloud Run と Apps Script で一致しているか確認

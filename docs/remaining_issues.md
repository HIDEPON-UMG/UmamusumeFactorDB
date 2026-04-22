# OCR 精度の残課題と次プラン候補

最終更新: 2026-04-22（Plan 1 実施後に整理）

## 0. 今日までの到達点

| 指標 | 値 | メモ |
|---|---|---|
| ★精度 | **32/37 (86.5%)** | ベースライン 27/37 から +5 件 |
| 因子名精度 | **17/37 = 46%**（誤認 20 件） | 従来 24 件から −4 件。Exp 1+7+4 で −1 件 |
| 悪化 | **0 件** | 現 28 件の正解は全て維持 |
| Cloud Run リビジョン | `factor-processor-00015-29k` | commit `9131b0d` 反映済（Plan 1 以降は未デプロイ） |
| 主要改修 | CNN 分類器導入 / row 0 位置絶対化 / rank fallback ガード / 赤 OCR allowlist / 青 OCR allowlist / 緑 rapidfuzz 重み調整 / 色判定閾値 0.20 / 緑断片 OCR + 緑専用マージ閾値 0.5 + 断片文字数補正 | — |

### 2026-04-22 Plan 1 の結果

| 施策 | 期待 | 実測 |
|---|---|---|
| D1: 色判定閾値 0.25 → 0.20 | 緑色判定失敗を救済 | ★+1（sample_oguricap main/green ★0→★2 解消） |
| F1: 青因子 OCR allowlist 実装 | 青 +1〜2 件 | 0 件（ONNX 側の系統誤認が優勢） |
| G2: 緑 rapidfuzz 重み 0.6/0.4 → 0.4/0.6 | 緑 +2〜3 件 | 0 件（top1 の寄せ先パターンは変わったが件数不変） |

### 2026-04-22 Plan 2（ONNX 温存実験群）の結果

| 施策 | 期待 | 実測 |
|---|---|---|
| Exp 1: 緑 OCR readtext(detail=1) + 断片別辞書マッチ | 緑 +2〜4 件 | 単独では 0 件。Exp 7 と組み合わせ必須と判明 |
| Exp 7: `_merge_candidates` の緑専用 ocr_strong_threshold=0.5 | 赤/青/緑 +1〜3 件 | 緑 +1 件（CHERRY☆スクランブル 解消）。 OCR top1 が 0.7 未満でも先頭昇格する |
| Exp 4: 断片経路の文字数比補正（len(frag)/len(name)） | 緑 +2〜3 件 | 単独の計測は未実施。Exp 1 の副作用（"Joy" → "Joyful Voyage!"）を抑制 |

**診断で判明した真因**（scripts/diagnose_green_fragments.py）:

- 緑誤認 12 件のうち **OCR + rapidfuzz は既に正解を top1 に出している**ケースが 5 件（CHERRY☆スクランブル / Joy to the World ×2 / 勝利の鼓動 / 勝利ヘ至ル累積）
- それらは `_merge_candidates` の `ocr_strong_threshold = 0.7` の壁に阻まれ、OCR top1 スコア 0.5-0.66 では ONNX 側に押し負けていた
- Exp 7 で閾値を 0.5 に緩和したことで CHERRY☆スクランブル は解消
- 残り 4 件（勝利ヘ至ル累積、Joy to the World ×2、勝利の鼓動）も OCR top1 は 0.57-0.73 で閾値クリアしているが、解消されていない。おそらく **複数の緑 box のうち誤った box が先に採用されている**（box 順序 or gold_star_count 優先順位の問題）

## 1. ★数認識の残課題（6 件）

### A. 過剰検出（HSV 段階の偽陽性）— 3 件

| # | image | role / color | 現 → 正解 | 真因 |
|---|---|---|---|---|
| A1 | receipt_20260421031432408.png | main / red | ★2 → ★1 | HSV 金★ 2 個検出、CNN conf=1.0 で 2 個とも gold、1 個は偽陽性 |
| A2 | receipt_20260421031558457.png | main / green | ★3 → ★2 | HSV 金★ 3 個検出、1 個が緑タイル内の偽陽性 |
| A3 | receipt_20260421031814474.png | main / green | ★2 → ★1 | 同上 |

**共通特徴**: 偽陽性★は CNN 学習データでは★と酷似しているため `gold` 判定される。ピッチ 14-22 px の空間配置フィルタも素通し（偽陽性が等間隔上にある）。

**対策候補**:
- B1. CNN 学習データに「★に酷似した UI 要素」の negative サンプルを 20-30 件追加し、`empty` クラスで再学習
- B2. ★スロットは UI 上必ず 3 個なので、`empty` 含めた連結成分数が 3 を超える場合のみ偽陽性排除（4 番目以降は除外）
- B3. タイル右端からの距離で絞る（★は右寄りに密集、左端付近の金色は偽陽性）

### B. 過少検出（HSV 金★取りこぼし）— 2 件

| # | image | role / color | 現 → 正解 | 真因 |
|---|---|---|---|---|
| B1 | receipt_20260421031814474.png | parent1 / green | ★1 → ★2 | HSV 金★ 1 個検出、実際は 2 個。1 個取りこぼし |
| B2 | receipt_20260421031851324.png | parent1 / blue | ★1 → ★3 | HSV 金★ 1 個検出、実際は 3 個。2 個取りこぼし |

**共通特徴**: HSV `(15-40, 120-255, 180-255)` のマスクから外れる暗め金★。過去 5 パターンの HSV 閾値緩和を試したが、いずれも他画像で悪化発生。

**対策候補**:
- C1. **空★位置から逆算して金★を推定**: 空★3 スロットが確定すれば、金★が取りこぼされたスロットの局所領域のみ閾値緩和で再検出
- C2. **CNN の検出面積拡張**: 現在 CNN は「HSV が拾った候補」だけを分類。タイル内の等間隔 3 スロット全てに CNN を走らせ、empty/gold を判定するフルスキャン方式に変更
- C3. **★3 スロット位置確定 + CNN 全スロット判定**: 最も強力だが実装コスト大

### C. 色判定ミス（★スロットに到達しない）— 0 件（2026-04-22 解消）

~~C1. sample_oguricap.png / main / green: ★0 → ★2~~ → D1（閾値 0.25 → 0.20）で解消。

残対策候補（将来別画像で再発した時のため温存）:

- D2. **位置＋空★数による緑推定**: row >= 1 で empty_star_count >= 2 の行は緑候補として green_ok を通す（Step 1 で試したが★3 誤認発生。gold=0 限定＋rank fallback スキップの厳格化で再挑戦の余地）
- D3. 緑因子タイル左端の黄色●アイコンを検出して、そこにある行を緑と強制判定

---

## 2. 因子名認識の残課題（21 件）

### D. 赤因子（距離 / 脚質 / バ場）— 5 件

| # | image | role | 現 → 正解 | ONNX top1 conf | 真因仮説 |
|---|---|---|---|---|---|
| D1 | combine_2026-01-22_17-04-20.png | main | 追込 → 先行 | 低 | ONNX 誤答 |
| D2 | receipt_20260421031432408.png | main | 芝 → マイル | **0.9996** | ONNX 高確信で誤答（最悪ケース） |
| D3 | receipt_20260421031755150.png | main | 芝 → 中距離 | 0.278 | ONNX 誤答 |
| D4 | receipt_20260421031851324.png | parent2 | 追込 → 長距離 | 1e-5 (極低) | ONNX 全候補が低 conf、ランダムに近い |
| D5 | receipt_20260421032331541.png | parent2 | 逃げ → ダート | 1e-4 (極低) | 同上 |

**共通特徴**:
- OCR 出力はすべて空か雑音 (`'2'`, `']'`, `'ぎ:永'` 等) で機能していない
- ONNX 推論が RED_FACTOR_TYPES 10 種の中で誤答
- 特に D2 は ONNX が 0.9996 で誤答しており、通常の信頼度フィルタでは除外不可

**対策候補**（効果期待の高い順）:
- E1. **factor ONNX モデルの再訓練**: 受領画像から赤因子 crop を収集し、距離/脚質/バ場の分類精度を上げる。学習データ 200-500 件が現実的。工数 2-3 日
- E2. **crop 位置の見直し**: 現在 box.bbox は★中心基準で 175×27 px。因子名テキスト部分の縦方向位置がズレているなら、赤専用に上下パディング調整
- E3. **サブカテゴリ分離**: RED_FACTOR_TYPES を「距離 4」「脚質 4」「バ場 2」の 3 クラスに分け、階層推論（まずどのサブカテゴリか判定 → その中で最適候補）
- E4. **低 confidence → レビューキュー**: ONNX top1 conf < 0.5 のレコードは自動書き込みせず review_queue へ（精度は上がらないが誤書き込みを回避）

### E. 青因子（ステータス 5 種）— 4 件

| # | image | role | 現 → 正解 |
|---|---|---|---|
| E1 | receipt_20260421031733727.png | parent2 | スピード → 賢さ |
| E2 | receipt_20260421031755150.png | parent2 | パワー → スピード |
| E3 | receipt_20260421031851324.png | parent1 | 根性 → スピード |
| E4 | receipt_20260421032331541.png | parent1 | パワー → スピード |

**特徴**:
- 青因子は ONNX 摂動アンサンブルで比較的安定しているが、特定画像で系統的に誤認
- BLUE_FACTOR_TYPES = ["スピード","スタミナ","パワー","根性","賢さ"] の 5 種内で混同

**対策候補**:
- F1. 青因子専用の allowlist OCR（赤と同様）: 「スピードスタミナパワー根性賢さ」のみ認識
- F2. ONNX 再訓練（E1 と共通の訓練フローに乗せる）

### F. 緑因子（固有スキル）— 12 件

| # | image | role | 現 → 正解（OCR出力と比較） |
|---|---|---|---|
| F1 | receipt_1432 | parent1 | 勝利ヘ至ル累積 → ずっとずっと輝いて |
| F2 | receipt_1432 | parent2 | Joy to the World → キラキラ☆STARDOM |
| F3 | receipt_1558 | main | 烈華の洗礼 → 精神一到何事か成らざらん |
| F4 | receipt_1558 | parent1 | 恵福バルカローレ → 羅刹、赤翼にて天上へ至らん |
| F5 | receipt_1558 | parent2 | Road to Glory → 羅刹、赤翼にて天上へ至らん |
| F6 | receipt_1733 | parent2 | 尊み☆ﾗｽﾄｽﾊﾟ—(ﾟ∀ﾟ)—ﾄ! → 精神一到何事か成らざらん |
| F7 | receipt_1814 | main | 演舞・撫子大薙刀 → 羅刹、赤翼にて天上へ至らん |
| F8 | receipt_1814 | parent1 | Shadow Break → 最強の名を懸けて |
| F9 | receipt_1832 | parent1 | CHERRY☆スクランブル → Presents from X |
| F10 | receipt_2331 | main | Joy to the World → 無二無三なる一条の路 |
| F11 | receipt_2331 | parent1 | 決意一筆 → ポンテ・デ・ディアマン |
| F12 | sample_oguricap | main | 勝利の鼓動 → (empty) |

**特徴**:
- 緑因子 = 固有スキル、249 種類の辞書から選択
- 記号や英字混じりが多く、OCR と rapidfuzz の組み合わせで誤マッチ多発
- F3, F4, F5, F7 が「羅刹、赤翼にて天上へ至らん」「精神一到何事か成らざらん」という特定の固有スキルに集中寄せされており、**辞書マッチのアンカーが偏っている**疑い
- F12 は empty（認識結果無し）

**対策候補**:
- G1. **緑因子の★位置ベース box 境界**: 緑タイルの左端黄色●アイコンを除外した★右端までの正確な box を取る
- G2. **rapidfuzz の重み調整**: 現 `partial*0.6 + ratio*0.4` を `partial*0.4 + ratio*0.6` に変え、部分一致ばかり拾わないようにする
- G3. **OCR 出力の複数候補 top-k マージ**: 現在 OCR 生テキスト 1 つに対して fuzzy 検索。EasyOCR readtext の detail=1 で複数テキスト断片を取得し、それぞれ辞書マッチして統合
- G4. **緑因子名の pre-computed embeddings + OCR 生テキストの近傍検索**: 編集距離より意味的な近さ（Sentence Embeddings）の方が有効な可能性

---

## 3. 次プラン候補（明日以降）

### Plan 1: 低リスク・高効果（半日〜1 日） — **2026-04-22 実施済**

1. ~~F1. 青因子 allowlist OCR 実装~~ → 実装済。単独での精度寄与は 0 件（ONNX 側が優勢）だが、将来的な ONNX 再訓練後の保険として温存
2. E4. 低 confidence レビューキュー化 → 本 Plan ではスキップ（精度指標に影響せず、Plan 4 に集約）
3. ~~D1. 色判定閾値チューニング~~ → 実施済。★+1 件（sample_oguricap 救済）
4. ~~G2. rapidfuzz 重み調整~~ → 実施済。寄せ先が変わったが件数は変化なし

**実測**: ★精度 31/37 → 32/37、名前誤認 21 件のまま。残りは ONNX 系統誤認の壁。

### Plan 2: 中リスク・中効果（1-2 日）— **次の本命**

5. **E1. factor ONNX 再訓練**
   - 受領画像から赤/青因子の crop を自動抽出 → labels.csv の correct_value でラベル付け → fine-tune
   - 学習データ: 受領 12 画像 × 3 ウマ娘 × 2 (青・赤) = 72 件ベース。データ拡張で 300-500 件
   - 工数: crop 抽出スクリプト 3h + 学習スクリプト 3h + 学習実行 1h + 評価 2h = 9h

**期待効果**: 赤 +3 件、青 +2 件、合計 +5 件

### Plan 3: 高コスト・根本対策（数日〜1 週間）

6. **C3. ★3 スロット全スキャン方式**
   - タイル内の等間隔 3 スロット位置を HSV に頼らず推定 → 全 3 スロットを CNN で empty/gold 判定
   - HSV 取りこぼしが完全に解消される可能性
   - 工数: 設計 + 実装 + 検証で 2-3 日

**期待効果**: ★精度 32/37 → 36/37（+4 件、B1/B2 の過少検出と A1〜A3 の過剰検出を両取り）

### Plan 4: 保険的対策

7. **E4. ReviewQueue 強化**: confidence ベースで全誤認候補を手動レビューに回す。これは精度を上げないが、本番への誤データ流入を防ぐ

---

## 4. 優先順位の推奨

1. ~~**Plan 1**（青 allowlist + 緑 fuzzy 調整 + 閾値チューニング）~~ — 2026-04-22 実施済。★+1 件
2. **Plan 2**（ONNX 再訓練）— 赤 5 件・青 4 件・緑 12 件の大半に直結。次に着手するならここ
3. **Plan 3**（★3 スロット全スキャン）— ★精度の天井突破が狙えるが大きめの改修
4. **Plan 4**（ReviewQueue 強化）— 上記と並行で進めても副作用無し

---

## 5. 参考：改善履歴

| コミット | 改善内容 | ★精度 | 名前誤認 |
|---|---|---|---|
| `69a41fe` | 空★検出導入（HSV マスク） | 27/37 | 24 |
| — | CNN 分類器統合（未コミット時） | 28/37 | 24 |
| `f79b712` | Step 2: row 0 位置絶対化 + rank fallback ガード | 31/37 | 23 |
| `9131b0d` | Step 5: 赤因子 OCR allowlist | 31/37 | **21** |
| *(未コミット)* | Plan 1: 青 OCR allowlist + 緑 rapidfuzz 重み + 色判定閾値 0.20 | **32/37** | 21 |

---

## 6. 参考：診断スクリプトの使い方

```bash
cd c:/Users/{UserName}/OneDrive/ドキュメント/ProjectFolders/UmamusumeFactorDB

# 現状の認識精度
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/evaluate_labels.py \
  --labels tests/fixtures/labels_2026-04-20T18-54-21.csv \
  --after tests/fixtures/colored_factors/recognition_results.json

# 因子名誤認の内訳（色別）
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/diagnose_name_errors.py

# ★検出の内部状態
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/diagnose_star_errors.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/diagnose_red_zero.py

# 赤因子の ONNX/OCR 候補スコア
PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/diagnose_red_candidates.py
```

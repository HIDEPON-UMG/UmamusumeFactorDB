# colored_factors — 色付き因子 OCR 精度評価用フィクスチャ

色付き因子（青・赤・緑）の OCR/ONNX 推論精度を測定・回帰テストするためのサンプル画像と正解ラベルを置く場所。

## ディレクトリ構成

```
tests/fixtures/colored_factors/
├── README.md              # このファイル
├── labels.csv             # 正解ラベル（後日スキーマ確定）
├── blue/                  # 青因子注目ケース
├── red/                   # 赤因子注目ケース（特に距離カテゴリ）
└── green/                 # 緑因子注目ケース
```

## ファイル命名規則

### 色別サブディレクトリに置く場合
因子ボックス単体ではなく **因子画面フル画像**（`analyze_image()` に食わせる形）を置く。

```
{factor_no}_{color}_{主要な因子名}.png
例:
  2_red_長距離misread.png       # factor_no=2、distance 混同の代表例
  5_red_star_confusion.png      # factor_no=5、★数混同の代表例
  7_blue_star_misread.png       # factor_no=7、blue_star 誤認
  baseline_01.png               # 比較基準：正しく認識される通常画像
```

`factor_no` を先頭に置くことで bug_reports と紐付けやすくする。`baseline_*` は「正しく読めた」比較用。

## マスキング必須（個人情報保護）

**画像を配置する前に** 以下をマスキング（ぼかし／塗り潰し）すること：

- トレーナーID
- 連絡先（Discord 名など）の写り込み
- 他プレイヤーの名前が写る部分（貸出因子画面など）

マスキングは因子本体（青/赤/緑/白の因子名、★数、キャラアイコン）**には掛けない**。OCR の評価対象なので。

## labels.csv（スキーマは Step 4 で確定）

暫定的な想定スキーマ：

```csv
image_path,uma_role,color,expected_name,expected_star,notes
blue/7_blue_star_misread.png,main,blue,スピード,1,bug_reports: blue_star 2→1
red/2_red_長距離misread.png,parent2,red,中距離,2,bug_reports: red_type 長距離→中距離
...
```

## 想定する収集対象（2026-04-21 時点）

### 最優先（bug_reports にある誤認元）
- factor_no=2（★数 4 件 + red_type 1 件の誤認。距離カテゴリ混同の実例）
- factor_no=5（red_star 2 件）
- factor_no=7（blue_star 1 件）

### 距離カテゴリ仮説の検証用
短距離 / マイル / 中距離 / 長距離 の赤因子が親または祖に含まれる画像を **2〜3 枚**。

### 比較基準
「正しく読めている」通常画像を **1〜2 枚**。

合計 **4〜6 枚** を目安。

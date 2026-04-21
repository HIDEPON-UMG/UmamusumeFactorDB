"""学習済 ★ CNN で全サンプルを推論し、疑わしいものを review フォルダに抽出する。

以下を 4 倍拡大して datasets/stars/review/ 配下の該当フォルダへ**コピー**する
（元ファイルは移動しないので、レビュー後は元フォルダ gold/ empty/ 内のファイルを
手動で移動・削除する）:

- review/should_be_empty/    : 自動ラベル gold だが CNN は empty と予測（偽陽性疑い）
- review/should_be_gold/     : 自動ラベル empty だが CNN は gold と予測（暗め金★の取りこぼし疑い）
- review/uncertain_gold/     : gold ラベルのまま信頼度が低い（<0.7）
- review/uncertain_empty/    : empty ラベルのまま信頼度が低い（<0.7）

拡大コピーは見やすさのため。ラベル変更するときは必ず元画像
(datasets/stars/gold/XXX.png または datasets/stars/empty/XXX.png) を
エクスプローラで操作する。

使い方:
    python scripts/review_star_labels.py
    python scripts/review_star_labels.py --confidence-threshold 0.6
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SLOT_SIZE = 28
SCALE = 4  # 拡大倍率


def imread_unicode(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def imwrite_unicode(path: Path, img) -> bool:
    ok, buf = cv2.imencode(path.suffix, img)
    if not ok:
        return False
    buf.tofile(str(path))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "datasets" / "stars",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=PROJECT_ROOT / "models" / "modules" / "star_classifier" / "prediction.onnx",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.7,
        help="この値未満は uncertain として抽出",
    )
    args = parser.parse_args()

    labels_csv = args.dataset / "labels.csv"
    if not labels_csv.exists():
        print(f"labels.csv が見つかりません: {labels_csv}", file=sys.stderr)
        sys.exit(1)

    sess = ort.InferenceSession(str(args.model), providers=["CPUExecutionProvider"])
    class_names = ["empty", "gold"]

    # review フォルダを初期化
    review_root = args.dataset / "review"
    if review_root.exists():
        shutil.rmtree(review_root)
    bins = {
        "should_be_empty": review_root / "should_be_empty",
        "should_be_gold": review_root / "should_be_gold",
        "uncertain_gold": review_root / "uncertain_gold",
        "uncertain_empty": review_root / "uncertain_empty",
    }
    for p in bins.values():
        p.mkdir(parents=True, exist_ok=True)

    # 一括推論
    items: list[tuple[Path, str, str]] = []  # (path, auto_label, filename)
    with labels_csv.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row["label"]
            if label not in class_names:
                continue
            p = args.dataset / label / row["filename"]
            if p.exists():
                items.append((p, label, row["filename"]))

    print(f"推論対象: {len(items)} 件")

    BATCH = 128
    results: list[tuple[str, float]] = []
    for i in range(0, len(items), BATCH):
        batch_imgs = []
        for path, _, _ in items[i : i + BATCH]:
            img = imread_unicode(path)
            if img is None:
                img = np.zeros((SLOT_SIZE, SLOT_SIZE, 3), dtype=np.uint8)
            if img.shape[:2] != (SLOT_SIZE, SLOT_SIZE):
                img = cv2.resize(img, (SLOT_SIZE, SLOT_SIZE), interpolation=cv2.INTER_AREA)
            batch_imgs.append(img.astype(np.uint8))
        batch = np.stack(batch_imgs, axis=0)
        outs = sess.run(["index", "confidence"], {"images": batch})
        for idx, conf in zip(outs[0].tolist(), outs[1].tolist()):
            results.append((class_names[int(idx)], float(conf)))

    # 分類して review フォルダへコピー
    counts = {k: 0 for k in bins}
    summary_csv = review_root / "review.csv"
    with summary_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "auto_label", "cnn_pred", "confidence", "bin"])
        for (path, auto_label, filename), (cnn_pred, conf) in zip(items, results):
            bin_name: str | None = None
            if auto_label != cnn_pred and conf >= 0.5:
                bin_name = "should_be_empty" if auto_label == "gold" else "should_be_gold"
            elif conf < args.confidence_threshold:
                bin_name = f"uncertain_{auto_label}"

            if bin_name is None:
                continue

            img = imread_unicode(path)
            if img is None:
                continue
            h, w = img.shape[:2]
            enlarged = cv2.resize(
                img, (w * SCALE, h * SCALE), interpolation=cv2.INTER_NEAREST
            )
            # 視認性: 予測情報をファイル名に埋め込む
            out_name = f"{cnn_pred}_{conf:.2f}__{filename}"
            out_path = bins[bin_name] / out_name
            imwrite_unicode(out_path, enlarged)
            counts[bin_name] += 1
            writer.writerow([filename, auto_label, cnn_pred, f"{conf:.4f}", bin_name])

    print("\n=== 抽出結果 ===")
    for name, n in counts.items():
        print(f"  {name}: {n} 件 → {bins[name]}")
    print(f"\nサマリ: {summary_csv}")
    print(
        "\n手順:\n"
        "  1. datasets/stars/review/ の各フォルダを Explorer のサムネイル表示で確認\n"
        "  2. 誤っている画像（例: should_be_empty/ の中で実際には gold だったもの）を特定\n"
        "  3. 元画像 datasets/stars/gold/<filename> または empty/<filename> を\n"
        "     Explorer で正しいフォルダへ移動、または明らかなノイズは削除\n"
        "  4. python scripts/rebuild_star_labels.py で labels.csv を再生成\n"
        "  5. python scripts/train_star_classifier.py で再学習"
    )


if __name__ == "__main__":
    main()

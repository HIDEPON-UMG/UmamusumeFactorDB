"""datasets/stars/gold/ と empty/ の現状から labels.csv を再生成する。

review_star_labels.py で抽出した疑わしいサンプルを手動で移動・削除した後、
現フォルダ構成を正として labels.csv を作り直すためのユーティリティ。
元 labels.csv はバックアップ (labels.csv.bak) として保存する。

使い方:
    python scripts/rebuild_star_labels.py
"""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "datasets" / "stars")
    args = parser.parse_args()

    labels_csv = args.dataset / "labels.csv"
    if labels_csv.exists():
        backup = args.dataset / "labels.csv.bak"
        shutil.copy2(labels_csv, backup)
        print(f"バックアップ: {backup}")

    # 元 CSV から x,y,w,h などのメタデータを引き継ぐため辞書化
    meta: dict[str, dict[str, str]] = {}
    if labels_csv.exists():
        with labels_csv.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meta[row["filename"]] = row

    # 現フォルダ構成から再構築
    new_rows = []
    for label in ("empty", "gold"):
        folder = args.dataset / label
        if not folder.exists():
            continue
        for img_path in sorted(folder.glob("*.png")):
            fname = img_path.name
            base = meta.get(fname, {})
            new_rows.append({
                "filename": fname,
                "label": label,
                "source_image": base.get("source_image", ""),
                "x": base.get("x", ""),
                "y": base.get("y", ""),
                "w": base.get("w", ""),
                "h": base.get("h", ""),
            })

    with labels_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["filename", "label", "source_image", "x", "y", "w", "h"]
        )
        writer.writeheader()
        writer.writerows(new_rows)

    counts = {"empty": 0, "gold": 0}
    for r in new_rows:
        counts[r["label"]] += 1
    print(f"\n=== 再生成完了 ===")
    print(f"gold: {counts['gold']} 件")
    print(f"empty: {counts['empty']} 件")
    print(f"labels.csv: {labels_csv}")


if __name__ == "__main__":
    main()

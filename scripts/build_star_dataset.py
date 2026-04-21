"""★スロット分類器用の学習データを自動生成する。

tests/fixtures/ 配下の PNG 画像から金★・空★の候補を HSV 検出し、
28x28 にパディング付きでクロップして datasets/stars/{gold,empty}/ に保存する。

自動ラベリングはあくまで初期値。取りこぼした暗め金★や、緑タイル左端の
黄色●（偽陽性）は notebooks/review_star_labels.ipynb で手動修正する。

使い方:
    python scripts/build_star_dataset.py
    python scripts/build_star_dataset.py --fixtures-dir tests/fixtures --out datasets/stars
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umafactor.cropper import (  # noqa: E402
    BASE_WIDTH,
    _detect_golden_stars,
    _detect_empty_stars,
    normalize_width,
)

SLOT_SIZE = 28  # ★スロット画像サイズ（CNN 入力）
PAD = 4  # ★bbox 周辺に確保する余白（リサイズ前の座標系）


def imread_unicode(path: Path) -> np.ndarray | None:
    """OpenCV の imread は Windows の日本語パスで失敗するため np.fromfile で読み込む。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None


def imwrite_unicode(path: Path, img: np.ndarray) -> bool:
    """OpenCV の imwrite も Windows 日本語パスで失敗することがあるため encode + tofile。"""
    ok, buf = cv2.imencode(path.suffix, img)
    if not ok:
        return False
    buf.tofile(str(path))
    return True


def crop_slot(img: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    """★bbox 中心から SLOT_SIZE に収まる正方形クロップを 28x28 で返す。"""
    cx, cy = x + w // 2, y + h // 2
    half = max(w, h) // 2 + PAD
    x0 = max(0, cx - half)
    x1 = min(img.shape[1], cx + half)
    y0 = max(0, cy - half)
    y1 = min(img.shape[0], cy + half)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return np.zeros((SLOT_SIZE, SLOT_SIZE, 3), dtype=np.uint8)
    return cv2.resize(crop, (SLOT_SIZE, SLOT_SIZE), interpolation=cv2.INTER_AREA)


def process_image(
    src_path: Path,
    out_root: Path,
    writer: csv.writer,
) -> tuple[int, int]:
    """1 枚の画像から金★/空★をクロップし保存。戻り値は (gold_count, empty_count)。"""
    img_orig = imread_unicode(src_path)
    if img_orig is None:
        print(f"[skip] 読み込み失敗: {src_path}", file=sys.stderr)
        return 0, 0
    img_norm, _ = normalize_width(img_orig, BASE_WIDTH)

    gold = _detect_golden_stars(img_norm)
    empty = _detect_empty_stars(img_norm)

    src_stem = src_path.stem
    gold_dir = out_root / "gold"
    empty_dir = out_root / "empty"
    gold_dir.mkdir(parents=True, exist_ok=True)
    empty_dir.mkdir(parents=True, exist_ok=True)

    for idx, (x, y, w, h) in enumerate(gold):
        slot = crop_slot(img_norm, x, y, w, h)
        fname = f"{src_stem}_gold{idx:03d}.png"
        out_path = gold_dir / fname
        imwrite_unicode(out_path, slot)
        writer.writerow([fname, "gold", src_stem, x, y, w, h])

    for idx, (x, y, w, h) in enumerate(empty):
        slot = crop_slot(img_norm, x, y, w, h)
        fname = f"{src_stem}_empty{idx:03d}.png"
        out_path = empty_dir / fname
        imwrite_unicode(out_path, slot)
        writer.writerow([fname, "empty", src_stem, x, y, w, h])

    return len(gold), len(empty)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=PROJECT_ROOT / "tests" / "fixtures",
        help="スクショ画像を再帰的に集めるディレクトリ",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "datasets" / "stars",
        help="出力先（gold/ と empty/ サブフォルダが作られる）",
    )
    parser.add_argument(
        "--pattern",
        default="*.png",
        help="画像ファイルの glob パターン",
    )
    args = parser.parse_args()

    images = sorted(
        p for p in args.fixtures_dir.glob(args.pattern)
        if p.is_file() and not p.name.startswith(".")
    )
    if not images:
        print(f"画像が見つかりません: {args.fixtures_dir}/{args.pattern}", file=sys.stderr)
        sys.exit(1)

    args.out.mkdir(parents=True, exist_ok=True)
    labels_csv = args.out / "labels.csv"

    total_gold = 0
    total_empty = 0
    with labels_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "label", "source_image", "x", "y", "w", "h"])
        for img_path in images:
            g, e = process_image(img_path, args.out, writer)
            total_gold += g
            total_empty += e
            print(f"{img_path.name}: gold={g}, empty={e}")

    print(f"\n=== 生成完了 ===")
    print(f"対象画像: {len(images)} 枚")
    print(f"gold: {total_gold} 枚 → {args.out / 'gold'}")
    print(f"empty: {total_empty} 枚 → {args.out / 'empty'}")
    print(f"labels.csv: {labels_csv}")
    print(
        "\n次のステップ: notebooks/review_star_labels.ipynb を起動し、"
        "取りこぼした暗め金★や偽陽性を手動で修正してください。"
    )


if __name__ == "__main__":
    main()

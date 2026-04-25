"""expected_labels の正解 crop を集めて、赤/青因子のテンプレート画像セットを作る。

出力: datasets/red_blue_templates/{red,blue}/<label>/<image>__<role>.png
各画像は 128×16 に正規化（縦横比維持せずリサイズ）し、テンプレマッチングで使う。
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from umafactor.cropper import (
    BASE_WIDTH, detect_chara_sections, extract_factor_boxes, normalize_width,
)
from umafactor.pipeline import _display_crop_from_original

EXPECTED = Path("tests") / "fixtures" / "expected_labels.csv"
OUT_DIR = Path("datasets") / "red_blue_templates"
TEMPLATE_SIZE = (128, 16)  # (W, H)

ROLES = ["main", "parent1", "parent2"]


def _imwrite_ja(path: Path, img):
    """cv2.imwrite は Windows の日本語パスで失敗するため imencode で保存。"""
    ok, buf = cv2.imencode(".png", img)
    if ok:
        path.write_bytes(buf.tobytes())


def _crop_for(img_orig, norm, scale, boxes, uma_idx: int, col_idx: int, is_red: bool):
    row_boxes = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 0 and b.col_index == col_idx]
    if not row_boxes:
        return None
    b = row_boxes[0]
    x0, y0, x1, y1 = b.bbox
    if is_red:
        img_h = norm.shape[0]
        bbox = (x0, y0, x1, min(img_h, y1 + 14))
        return _display_crop_from_original(img_orig, bbox, scale, pad_y_norm=2)
    else:
        return _display_crop_from_original(img_orig, b.bbox, scale, pad_y_norm=8)


def main() -> int:
    with EXPECTED.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    (OUT_DIR / "red").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "blue").mkdir(parents=True, exist_ok=True)

    saved_red = 0
    saved_blue = 0
    skipped = 0

    # 画像ごとに 1 回だけ crop を計算
    per_image_cache: dict[str, tuple] = {}

    for r in rows:
        image = r["image_name"]
        role = r["role"]
        uma_idx = ROLES.index(role)

        if image not in per_image_cache:
            try:
                img_orig = cv2.imdecode(
                    np.fromfile(f"tests/fixtures/{image}", dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                norm, scale = normalize_width(img_orig, BASE_WIDTH)
                sections = detect_chara_sections(norm)
                boxes = extract_factor_boxes(norm, sections)
                per_image_cache[image] = (img_orig, norm, scale, boxes)
            except Exception as e:
                print(f"skip {image}: {e}")
                per_image_cache[image] = None
                continue
        if per_image_cache[image] is None:
            skipped += 1
            continue
        img_orig, norm, scale, boxes = per_image_cache[image]

        # 赤因子
        red_crop = _crop_for(img_orig, norm, scale, boxes, uma_idx, col_idx=1, is_red=True)
        if red_crop is not None and red_crop.size > 0:
            label = r["red_type"]
            out = OUT_DIR / "red" / label
            out.mkdir(parents=True, exist_ok=True)
            resized = cv2.resize(red_crop, TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
            _imwrite_ja(out / f"{image.replace('.png','')}__{role}.png", resized)
            saved_red += 1

        # 青因子
        blue_crop = _crop_for(img_orig, norm, scale, boxes, uma_idx, col_idx=0, is_red=False)
        if blue_crop is not None and blue_crop.size > 0:
            label = r["blue_type"]
            out = OUT_DIR / "blue" / label
            out.mkdir(parents=True, exist_ok=True)
            resized = cv2.resize(blue_crop, TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
            _imwrite_ja(out / f"{image.replace('.png','')}__{role}.png", resized)
            saved_blue += 1

    print(f"red templates: {saved_red}, blue templates: {saved_blue}, skipped images: {skipped}")
    # ラベル別件数
    print("\nred labels:")
    for d in sorted((OUT_DIR / "red").iterdir()):
        if d.is_dir():
            print(f"  {d.name}: {len(list(d.glob('*.png')))} 枚")
    print("\nblue labels:")
    for d in sorted((OUT_DIR / "blue").iterdir()):
        if d.is_dir():
            print(f"  {d.name}: {len(list(d.glob('*.png')))} 枚")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

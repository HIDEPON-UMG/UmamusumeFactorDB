"""expected_labels の緑/青/赤因子 ★数 に対応する★領域 crop を集める。

出力: datasets/star_templates/{green,blue,red}/{1,2,3}/<image>__<role>.png
各画像は 64×16 に正規化（★3 個分の幅を想定）。

各タイルの★領域は display_crop の右半分を使って近似する
（UI 仕様上、各因子タイル右端に★3 スロットが等間隔で並ぶ）。
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
OUT_ROOT = Path("datasets") / "star_templates"
TEMPLATE_SIZE = (64, 16)  # (W, H) — ★3 個分の幅を取りたいので横長

ROLES = ["main", "parent1", "parent2"]


def _imwrite_ja(path: Path, img):
    ok, buf = cv2.imencode(".png", img)
    if ok:
        path.write_bytes(buf.tobytes())


def _crop_star_region(img_orig, norm, scale, boxes, uma_idx: int, category: str):
    """指定カテゴリ (green/blue/red) の box の ★領域を切り出す。

    位置特定:
      blue: row=0, col=0
      red : row=0, col=1
      green: color=='green' かつ col=0 (fallback: row=1, col=0)

    タイル右 50% を★領域として抽出。
    """
    if category == "blue":
        candidates = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 0 and b.col_index == 0]
    elif category == "red":
        candidates = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 0 and b.col_index == 1]
    elif category == "green":
        candidates = [b for b in boxes if b.uma_index == uma_idx and b.col_index == 0 and b.color == "green"]
        if not candidates:
            candidates = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 1 and b.col_index == 0]
    else:
        raise ValueError(category)
    if not candidates:
        return None
    b = candidates[0]
    x0, y0, x1, y1 = b.bbox
    right_x0 = x0 + int((x1 - x0) * 0.5)
    dc = _display_crop_from_original(img_orig, (right_x0, y0, x1, y1), scale, pad_y_norm=2)
    return dc


def main() -> int:
    with EXPECTED.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    categories = ("green", "blue", "red")
    for cat in categories:
        for s in (1, 2, 3):
            (OUT_ROOT / cat / str(s)).mkdir(parents=True, exist_ok=True)

    per_cat_star: dict[str, dict[int, int]] = {c: {1: 0, 2: 0, 3: 0} for c in categories}
    per_image_cache: dict[str, tuple] = {}

    for r in rows:
        image = r["image_name"]
        role = r["role"]
        uma_idx = ROLES.index(role)

        if image not in per_image_cache:
            try:
                img_orig = cv2.imdecode(
                    np.fromfile(f"tests/fixtures/{image}", dtype=np.uint8), cv2.IMREAD_COLOR,
                )
                norm, scale = normalize_width(img_orig, BASE_WIDTH)
                sections = detect_chara_sections(norm)
                boxes = extract_factor_boxes(norm, sections)
                per_image_cache[image] = (img_orig, norm, scale, boxes)
            except Exception as e:
                print(f"skip {image}: {e}")
                per_image_cache[image] = None
        if per_image_cache[image] is None:
            continue
        img_orig, norm, scale, boxes = per_image_cache[image]

        for cat in categories:
            star = int(r[f"{cat}_star"])
            if star not in (1, 2, 3):
                continue
            star_crop = _crop_star_region(img_orig, norm, scale, boxes, uma_idx, cat)
            if star_crop is None or star_crop.size == 0:
                continue
            resized = cv2.resize(star_crop, TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
            out = OUT_ROOT / cat / str(star) / f"{image.replace('.png','')}__{role}.png"
            _imwrite_ja(out, resized)
            per_cat_star[cat][star] += 1

    for cat in categories:
        total = sum(per_cat_star[cat].values())
        print(f"{cat}: 合計 {total} 枚 ({per_cat_star[cat]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

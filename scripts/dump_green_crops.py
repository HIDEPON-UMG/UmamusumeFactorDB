"""緑誤認 7 件について、uma 内のすべての緑 box を dump して位置と crop 内容を確認。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from umafactor.cropper import (  # noqa: E402
    BASE_WIDTH,
    detect_chara_sections,
    extract_factor_boxes,
    normalize_width,
)

CASES = [
    ("receipt_20260421031558457.png", "main", "烈華の洗礼"),
    ("receipt_20260421031558457.png", "parent1", "恵福バルカローレ"),
    ("receipt_20260421031558457.png", "parent2", "Road to Glory"),
    ("receipt_20260421031733727.png", "parent2", "尊み☆ﾗｽﾄｽﾊﾟ—(ﾟ∀ﾟ)—ﾄ!"),
    ("receipt_20260421031814474.png", "main", "演舞・撫子大薙刀"),
    ("receipt_20260421031814474.png", "parent1", "Shadow Break"),
    ("receipt_20260421032331541.png", "parent1", "決意一筆"),
]


def imread(p):
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def imwrite(p, img):
    ret, buf = cv2.imencode(p.suffix, img)
    if ret:
        buf.tofile(str(p))


def wide_crop(norm, bbox, pad=20):
    x0, y0, x1, y1 = bbox
    h, w = norm.shape[:2]
    return norm[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)]


def main():
    out_dir = ROOT / "tests" / "fixtures" / "debug_green_crops"
    out_dir.mkdir(parents=True, exist_ok=True)
    roles = {"main": 0, "parent1": 1, "parent2": 2}
    for img_name, role, correct in CASES:
        p = ROOT / "tests" / "fixtures" / img_name
        orig = imread(p)
        norm, scale = normalize_width(orig, BASE_WIDTH)
        sections = detect_chara_sections(norm)
        boxes = extract_factor_boxes(norm, sections)
        tui = roles[role]
        green_boxes = [b for b in boxes if b.uma_index == tui and b.color == "green"]
        print(f"\n=== {img_name} / {role} (正解: {correct}) ===")
        print(f"  緑 box 総数: {len(green_boxes)}")
        base = f"{img_name.replace('.png','')}_{role}"
        for i, b in enumerate(green_boxes):
            print(f"  box#{i} row={b.row_index} col={b.col_index} gold={b.gold_star_count} bbox={b.bbox}")
            wc = wide_crop(norm, b.bbox)
            imwrite(out_dir / f"{base}_box{i}_row{b.row_index}_col{b.col_index}_wide.png", wc)


if __name__ == "__main__":
    main()

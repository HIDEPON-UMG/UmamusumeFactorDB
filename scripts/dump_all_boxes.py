"""緑誤認画像について、色問わず全 boxes をダンプして緑検出失敗の真因を見る。"""
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
    detect_factor_color,
    extract_factor_boxes,
    normalize_width,
)

IMAGES = [
    ("receipt_20260421031558457.png", "1558"),
    ("receipt_20260421031814474.png", "1814"),
]

ROLES = {0: "main", 1: "parent1", 2: "parent2"}


def imread(p):
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def main():
    for img_name, tag in IMAGES:
        p = ROOT / "tests" / "fixtures" / img_name
        orig = imread(p)
        norm, scale = normalize_width(orig, BASE_WIDTH)
        sections = detect_chara_sections(norm)
        boxes = extract_factor_boxes(norm, sections)
        print(f"\n==== {img_name} ({tag}) ====")
        print(f"  sections: {[(s.uma_index, s.factor_y_start, s.factor_y_end) for s in sections]}")
        # group by uma_index
        for uidx in (0, 1, 2):
            print(f"  -- {ROLES[uidx]} --")
            uboxes = [b for b in boxes if b.uma_index == uidx]
            uboxes.sort(key=lambda b: (b.row_index, b.col_index))
            for b in uboxes:
                # 左端チップの HSV スコアを確認（色判定の入力）
                x0, y0, x1, y1 = b.bbox
                box_bgr = norm[y0:y1, x0:x1]
                # detect_factor_color 相当の計算をログ
                from umafactor.config import FACTOR_COLOR_HSV_RANGES
                h, w = box_bgr.shape[:2]
                chip = box_bgr[:, 0:max(4, int(w*0.15))]
                hsv = cv2.cvtColor(chip, cv2.COLOR_BGR2HSV)
                def ratio(lo, hi):
                    mask = cv2.inRange(hsv, np.array(lo, dtype=np.uint8), np.array(hi, dtype=np.uint8))
                    return float(mask.mean())/255.0
                scores = {
                    "blue": ratio(*FACTOR_COLOR_HSV_RANGES["blue"]),
                    "green": ratio(*FACTOR_COLOR_HSV_RANGES["green"]),
                    "red": ratio(*FACTOR_COLOR_HSV_RANGES["red"]),
                }
                print(f"    row={b.row_index} col={b.col_index} color={b.color} gold={b.gold_star_count} bbox={b.bbox}")
                print(f"      HSV scores: blue={scores['blue']:.3f} green={scores['green']:.3f} red={scores['red']:.3f}")


if __name__ == "__main__":
    main()

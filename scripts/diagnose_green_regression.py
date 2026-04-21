"""Step 1 で悪化した緑因子 ★2→★3 のメカニズムを特定する診断スクリプト。

analyze_image を debug 付きで走らせ、box 採用の前後を観察する。
具体的には extract_factor_boxes が返した各 FactorBox について
(uma_index, row_index, col_index, color, gold_star_count, empty_star_count)
を dump し、main/green（または悪化ターゲット）に採用された box を特定する。

使い方:
    python scripts/diagnose_green_regression.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import cv2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umafactor.cropper import (  # noqa: E402
    BASE_WIDTH,
    detect_chara_sections,
    extract_factor_boxes,
    normalize_width,
)


TARGETS = [
    ("receipt_20260421031432408.png", "main"),
    ("receipt_20260421031733727.png", "parent1"),
    ("receipt_20260421031832634.png", "parent1"),
    ("receipt_20260421031851324.png", "main"),
    ("receipt_20260421032331541.png", "main"),
]


def imread_unicode(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def main() -> None:
    fixtures = PROJECT_ROOT / "tests" / "fixtures"
    for img_name, role in TARGETS:
        path = fixtures / img_name
        img_orig = imread_unicode(path)
        if img_orig is None:
            continue
        img_norm, _ = normalize_width(img_orig, BASE_WIDTH)
        try:
            sections = detect_chara_sections(img_norm)
        except RuntimeError as e:
            print(f"{img_name}: セクション検出失敗 {e}")
            continue
        boxes = extract_factor_boxes(img_norm, sections)
        uma_target = {"main": 0, "parent1": 1, "parent2": 2}[role]
        print(f"\n=== {img_name} / {role} (uma_index={uma_target}) ===")
        print("box_idx | uma | row | col | color | gold | empty")
        for i, b in enumerate(boxes):
            if b.uma_index != uma_target:
                continue
            print(
                f"{i:3d}     | {b.uma_index}   | {b.row_index}   | {b.col_index}   | "
                f"{b.color:6s}| {b.gold_star_count}    | {b.empty_star_count}"
            )


if __name__ == "__main__":
    main()

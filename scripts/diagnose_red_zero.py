"""receipt_1814 / receipt_1832 の main/red ★0 誤認を追跡する。

analyze_image を走らせて、main/red に割り当てられた box の
(row_index, col_index, color, gold_star_count, confidence 等) をダンプ。
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
from umafactor.infer import get_predictor  # noqa: E402

TARGETS = [
    "receipt_20260421031814474.png",
    "receipt_20260421031832634.png",
    "receipt_20260421032331541.png",
    "sample_oguricap.png",
]


def imread_unicode(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def main() -> None:
    fixtures = PROJECT_ROOT / "tests" / "fixtures"
    rank_pred = get_predictor("factor_rank")
    for img_name in TARGETS:
        path = fixtures / img_name
        img_orig = imread_unicode(path)
        if img_orig is None:
            continue
        img_norm, scale = normalize_width(img_orig, BASE_WIDTH)
        try:
            sections = detect_chara_sections(img_norm)
        except RuntimeError as e:
            print(f"{img_name}: セクション検出失敗 {e}")
            continue
        boxes = extract_factor_boxes(img_norm, sections)
        print(f"\n=== {img_name} ===")
        print("box_idx | uma | row | col | color | gold | empty | rank_pred | rank_conf")
        for i, b in enumerate(boxes):
            if b.row_index > 2:
                continue
            # rank crop を再現
            rank_img = b.rank_img
            try:
                rpred = rank_pred.predict(rank_img)
                rlabel = rpred.label
                rconf = rpred.confidence
            except Exception as e:
                rlabel = f"err:{e}"
                rconf = 0.0
            print(
                f"{i:3d}     | {b.uma_index}   | {b.row_index}   | {b.col_index}   | "
                f"{b.color:6s}| {b.gold_star_count}    | {b.empty_star_count}     | "
                f"{rlabel:8s}  | {rconf:.3f}"
            )


if __name__ == "__main__":
    main()

"""指定画像の全 box を (uma, row, col, color, gold, empty) でダンプし、
緑スロット採用時の★集計状況を追跡する。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from umafactor.cropper import (
    detect_chara_sections,
    extract_factor_boxes,
    normalize_width,
    BASE_WIDTH,
)


def main(image_rel: str) -> int:
    img_orig = cv2.imdecode(
        import_numpy := __import__("numpy").fromfile(image_rel, dtype=__import__("numpy").uint8),
        cv2.IMREAD_COLOR,
    )
    if img_orig is None:
        print(f"ERROR: imread failed: {image_rel}")
        return 1
    norm, scale = normalize_width(img_orig, BASE_WIDTH)
    sections = detect_chara_sections(norm)
    boxes = extract_factor_boxes(norm, sections)
    print(f"# {image_rel}")
    print(f"  sections: {len(sections)}, boxes: {len(boxes)}")
    print(f"  {'uma':>3s} {'row':>3s} {'col':>3s} {'color':<8s} {'gold':>4s} {'empty':>5s}")
    for b in boxes:
        print(
            f"  {b.uma_index:>3d} {b.row_index:>3d} {b.col_index:>3d} "
            f"{b.color:<8s} {b.gold_star_count or 0:>4d} {b.empty_star_count or 0:>5d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/umamusume_20260424_180452_warn.png"))

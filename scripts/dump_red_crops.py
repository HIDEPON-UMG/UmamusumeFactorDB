"""赤/青誤認ケースの crop を debug_red_crops/ に dump し、目視確認用に保存。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umafactor.cropper import (  # noqa: E402
    BASE_WIDTH,
    detect_chara_sections,
    extract_factor_boxes,
    normalize_width,
)

# (image, role, color, correct) -- color は "red" or "blue"
CASES = [
    ("combine_2026-01-22_17-04-20.png", "main", "red", "先行"),
    ("receipt_20260421031432408.png", "main", "red", "マイル"),
    ("receipt_20260421031755150.png", "main", "red", "中距離"),
    ("receipt_20260421031851324.png", "parent2", "red", "長距離"),
    ("receipt_20260421032331541.png", "parent2", "red", "ダート"),
    ("receipt_20260421031733727.png", "parent1", "blue", "賢さ"),
    ("receipt_20260421031851324.png", "parent1", "blue", "スピード"),
    ("receipt_20260421032331541.png", "parent1", "blue", "スピード"),
    ("receipt_20260421032331541.png", "parent2", "blue", "スタミナ"),
]


def imread_unicode(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def imwrite_unicode(path: Path, img: np.ndarray) -> None:
    ret, buf = cv2.imencode(path.suffix, img)
    if ret:
        buf.tofile(str(path))


def main() -> None:
    fixtures = PROJECT_ROOT / "tests" / "fixtures"
    out_dir = PROJECT_ROOT / "tests" / "fixtures" / "debug_red_blue_crops"
    out_dir.mkdir(parents=True, exist_ok=True)

    role_to_idx = {"main": 0, "parent1": 1, "parent2": 2}

    for img_name, role, color, correct in CASES:
        path = fixtures / img_name
        img_orig = imread_unicode(path)
        if img_orig is None:
            print(f"skip: {img_name}")
            continue
        img_norm, scale = normalize_width(img_orig, BASE_WIDTH)
        try:
            sections = detect_chara_sections(img_norm)
        except RuntimeError:
            continue
        boxes = extract_factor_boxes(img_norm, sections)
        target_uma = role_to_idx[role]

        # 赤は row=0 col=1、青は row=0 col=0
        target_col = 1 if color == "red" else 0
        target_box = next(
            (b for b in boxes if b.uma_index == target_uma and b.row_index == 0 and b.col_index == target_col),
            None,
        )
        if target_box is None:
            print(f"{img_name} / {role} / {color}: box 見つからず")
            continue

        # 正規化画像側の crop（現行 bbox + pad_y=6 拡張版も出す）
        x0, y0, x1, y1 = target_box.bbox
        crop_norm = img_norm[y0:y1, x0:x1]
        ext_y1 = min(img_norm.shape[0], y1 + 6)
        crop_norm_ext6 = img_norm[y0:ext_y1, x0:x1]
        ext_y1_12 = min(img_norm.shape[0], y1 + 12)
        crop_norm_ext12 = img_norm[y0:ext_y1_12, x0:x1]
        # 元画像側の crop
        inv = 1.0 / scale if scale != 0 else 1.0
        ox0 = max(0, int(round(x0 * inv)))
        oy0 = max(0, int(round(y0 * inv)))
        ox1 = min(img_orig.shape[1], int(round(x1 * inv)))
        oy1 = min(img_orig.shape[0], int(round(y1 * inv)))
        crop_orig = img_orig[oy0:oy1, ox0:ox1]
        # 広めの周辺（パディング 20px norm）
        pad = 20
        wx0 = max(0, x0 - pad)
        wy0 = max(0, y0 - pad)
        wx1 = min(img_norm.shape[1], x1 + pad)
        wy1 = min(img_norm.shape[0], y1 + pad)
        crop_wide = img_norm[wy0:wy1, wx0:wx1]

        base = f"{img_name.replace('.png','')}_{role}_{color}_{correct}"
        imwrite_unicode(out_dir / f"{base}_norm.png", crop_norm)
        imwrite_unicode(out_dir / f"{base}_norm_ext6.png", crop_norm_ext6)
        imwrite_unicode(out_dir / f"{base}_norm_ext12.png", crop_norm_ext12)
        imwrite_unicode(out_dir / f"{base}_orig.png", crop_orig)
        imwrite_unicode(out_dir / f"{base}_wide_norm.png", crop_wide)
        print(
            f"  {base}: norm={crop_norm.shape}, orig={crop_orig.shape}, "
            f"bbox_norm={target_box.bbox}"
        )


if __name__ == "__main__":
    main()

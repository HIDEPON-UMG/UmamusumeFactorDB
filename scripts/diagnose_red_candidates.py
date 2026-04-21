"""赤因子の誤認 7 件について、ONNX と OCR の生候補スコアをダンプする。

どちらが「芝」等の誤答を返しているのかを特定し、対策方針を決める。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umafactor.cropper import BASE_WIDTH, detect_chara_sections, extract_factor_boxes, normalize_width  # noqa: E402
from umafactor.infer import get_predictor  # noqa: E402
from umafactor.ocr import get_ocr  # noqa: E402

RED_FACTOR_TYPES = [
    "逃げ", "先行", "差し", "追込",
    "短距離", "マイル", "中距離", "長距離",
    "芝", "ダート",
]
PERTURBATIONS_RED = [(0, 0), (0, -1), (0, 1), (-1, 0), (1, 0)]

CASES = [
    # (image, role, correct)
    ("combine_2026-01-22_17-04-20.png", "main", "先行"),
    ("receipt_20260421031432408.png", "main", "マイル"),
    ("receipt_20260421031755150.png", "main", "中距離"),
    ("receipt_20260421031814474.png", "main", "長距離"),
    ("receipt_20260421031851324.png", "parent2", "長距離"),
    ("receipt_20260421032331541.png", "parent2", "ダート"),
    ("sample_oguricap.png", "main", "芝"),
]


def imread_unicode(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def main() -> None:
    fixtures = PROJECT_ROOT / "tests" / "fixtures"
    factor_pred = get_predictor("factor")
    ocr = get_ocr()
    role_to_idx = {"main": 0, "parent1": 1, "parent2": 2}

    for img_name, role, correct in CASES:
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

        # 赤スロットは row 0 col 1
        red_box = next(
            (b for b in boxes if b.uma_index == target_uma and b.row_index == 0 and b.col_index == 1),
            None,
        )
        if red_box is None:
            print(f"{img_name} / {role}: red box 見つからず")
            continue

        # ONNX 候補（pipeline.py と同じ crops）
        x0, y0, x1, y1 = red_box.bbox
        text_crop_norm = img_norm[y0:y1, x0:x1]
        # 元画像からも crops（摂動 + scale 逆変換）
        inv = 1.0 / scale if scale != 0 else 1.0
        crops = []
        for dy, dx in PERTURBATIONS_RED:
            ox0 = max(0, int(round(x0 * inv)) + dx)
            oy0 = max(0, int(round(y0 * inv)) + dy)
            ox1 = min(img_orig.shape[1], int(round(x1 * inv)) + dx)
            oy1 = min(img_orig.shape[0], int(round(y1 * inv)) + dy)
            crops.append(img_orig[oy0:oy1, ox0:ox1])
        crops.append(text_crop_norm)
        onnx_candidates = factor_pred.topk_in_category(
            crops, RED_FACTOR_TYPES, k=5, use_multi_interp=True
        )

        # OCR 候補（display_crop は pipeline.py では広めだが、簡易に text_crop_norm x 元画像版を使用）
        from umafactor.pipeline import _display_crop_from_original

        display_crop = _display_crop_from_original(img_orig, red_box.bbox, scale)
        ocr_raw = ocr.recognize(display_crop)
        ocr_candidates_all = ocr.match_to_factor(ocr_raw, top_k=5)
        ocr_candidates = [(n, s) for n, s in ocr_candidates_all if n in RED_FACTOR_TYPES]

        print(f"\n=== {img_name} / {role} (正解: {correct}) ===")
        print(f"  OCR raw: {ocr_raw!r}")
        print(f"  ONNX topk: {onnx_candidates}")
        print(f"  OCR topk (red filtered): {ocr_candidates}")


if __name__ == "__main__":
    main()

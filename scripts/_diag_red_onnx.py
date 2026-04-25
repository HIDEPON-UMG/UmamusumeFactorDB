"""red_type Red 件について ONNX top-5 と OCR 結果を並べて原因を特定する。"""
from __future__ import annotations

import csv
import json
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
from umafactor.infer import get_predictor
from umafactor.ocr import get_ocr
from umafactor.pipeline import (
    PERTURBATIONS_RED, RED_FACTOR_TYPES,
    _crop_from_original, _display_crop_from_original,
)

EXPECTED = Path("tests") / "fixtures" / "expected_labels.csv"


def main() -> int:
    with EXPECTED.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # red_type Red 行
    rec = json.loads(Path("tests/fixtures/colored_factors/recognition_results.json").read_text(encoding="utf-8"))
    reds = []
    for r in rows:
        got = rec.get(r["image_name"], {}).get(r["role"], {}).get("red", {}).get("type", "")
        if got != r["red_type"]:
            reds.append((r["image_name"], r["role"], got, r["red_type"]))
    print(f"red_type Red: {len(reds)} 件")

    fp = get_predictor("factor")
    ocr = get_ocr()

    for image, role, got, exp in reds[:15]:
        img_orig = cv2.imdecode(
            np.fromfile(f"tests/fixtures/{image}", dtype=np.uint8), cv2.IMREAD_COLOR,
        )
        norm, scale = normalize_width(img_orig, BASE_WIDTH)
        sections = detect_chara_sections(norm)
        boxes = extract_factor_boxes(norm, sections)
        uma_idx = ["main", "parent1", "parent2"].index(role)
        # row=0 col=1 が red スロット
        red_boxes = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 0 and b.col_index == 1]
        if not red_boxes:
            print(f"\n{image[:40]:<40s} / {role:<7s}  [NO RED BOX] 期待={exp}, 認識={got}")
            continue
        box = red_boxes[0]
        x0, y0, x1, y1 = box.bbox
        text_crop_norm = norm[y0:y1, x0:x1]
        img_h = norm.shape[0]
        red_disp_bbox = (x0, y0, x1, min(img_h, y1 + 14))
        display_crop = _display_crop_from_original(img_orig, red_disp_bbox, scale, pad_y_norm=2)
        crops = [_crop_from_original(img_orig, box.bbox, scale, dy, dx) for dy, dx in PERTURBATIONS_RED]
        crops.append(text_crop_norm)
        onnx_cands = fp.topk_in_category(crops, RED_FACTOR_TYPES, k=5, use_multi_interp=True)
        ocr_raw = ocr.recognize_red(display_crop)
        ocr_cands = ocr.match_to_factor(ocr_raw, top_k=5)
        ocr_cands = [(n, s) for n, s in ocr_cands if n in RED_FACTOR_TYPES]

        print(f"\n{image[:40]:<40s} / {role:<7s}  期待={exp}  認識={got}")
        print(f"  ONNX top5: {[(n, f'{s:.2f}') for n, s in onnx_cands]}")
        print(f"  OCR raw  : {ocr_raw!r}")
        print(f"  OCR cands: {[(n, f'{s:.2f}') for n, s in ocr_cands]}")
        exp_rank = next((i for i, (n, _) in enumerate(onnx_cands) if n == exp), None)
        print(f"  正解の ONNX top 内順位: {exp_rank if exp_rank is not None else '圏外'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

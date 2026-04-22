"""緑因子誤認ケースで OCR の連結テキストと断片、match_to_green_factor_multi の
top-k 候補、正解ラベルを並べて出力する診断スクリプト。

Exp 1 の効果測定用。match_to_green_factor_multi が正解候補を上位に浮上させ
られているか、それとも fragments が短すぎて使われていないかを可視化する。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from umafactor.cropper import (  # noqa: E402
    detect_chara_sections,
    extract_factor_boxes,
    normalize_width,
)
from umafactor.ocr import get_ocr  # noqa: E402


def imread_unicode(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


# 誤認ケース（green のみ）を labels.csv から抽出
GREEN_TARGETS = [
    ("receipt_20260421031432408.png", "parent1"),
    ("receipt_20260421031432408.png", "parent2"),
    ("receipt_20260421031558457.png", "main"),
    ("receipt_20260421031558457.png", "parent1"),
    ("receipt_20260421031558457.png", "parent2"),
    ("receipt_20260421031733727.png", "parent2"),
    ("receipt_20260421031814474.png", "main"),
    ("receipt_20260421031814474.png", "parent1"),
    ("receipt_20260421031832634.png", "parent1"),
    ("receipt_20260421032331541.png", "main"),
    ("receipt_20260421032331541.png", "parent1"),
    ("sample_oguricap.png", "main"),
]

IMG_DIR = ROOT / "tests" / "fixtures"
LABELS = ROOT / "tests" / "fixtures" / "labels_2026-04-20T18-54-21.csv"


def load_labels() -> dict[tuple[str, str], str]:
    """(image_name, role) -> 緑因子の正解名"""
    result: dict[tuple[str, str], str] = {}
    with LABELS.open(encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            img = row.get("image_name", "")
            role = row.get("role", "")
            color = row.get("color", "")
            value = row.get("correct_value", "") or row.get("wrong_value", "")
            if color == "green" and (img, role) in GREEN_TARGETS:
                result[(img, role)] = value
    return result


def _display_crop(img_bgr: np.ndarray, bbox: tuple[int, int, int, int], scale: float) -> np.ndarray:
    inv = 1.0 / scale if scale != 0 else 1.0
    x0, y0, x1, y1 = bbox
    PAD_LEFT = 32
    PAD_RIGHT = 8
    PAD_Y = 2
    ox0 = int(round((x0 - PAD_LEFT) * inv))
    oy0 = int(round((y0 - PAD_Y) * inv))
    ox1 = int(round((x1 + PAD_RIGHT) * inv))
    oy1 = int(round((y1 + PAD_Y) * inv))
    ox0 = max(0, ox0)
    oy0 = max(0, oy0)
    ox1 = min(img_bgr.shape[1], ox1)
    oy1 = min(img_bgr.shape[0], oy1)
    return img_bgr[oy0:oy1, ox0:ox1]


def main() -> None:
    labels = load_labels()
    ocr = get_ocr()
    ROLES = ["main", "parent1", "parent2"]

    for img_name in sorted({t[0] for t in GREEN_TARGETS}):
        img_path = IMG_DIR / img_name
        img_orig = imread_unicode(img_path)
        norm, scale = normalize_width(img_orig)
        sections = detect_chara_sections(norm)
        boxes = extract_factor_boxes(norm, sections)

        # 緑 box を role ごとに最初の 1 個（green_name 用）として選ぶ
        green_by_role: dict[str, list] = {r: [] for r in ROLES}
        for box in boxes:
            if box.color != "green":
                continue
            role = ROLES[box.uma_index]
            green_by_role[role].append(box)

        print(f"\n========== {img_name} ==========")
        for role in ROLES:
            if (img_name, role) not in labels:
                continue
            correct = labels[(img_name, role)]
            print(f"--- {role} (正解: {correct}) ---")
            if not green_by_role[role]:
                print("  [緑 box なし]")
                continue
            # 各 green box で OCR 出力と候補を見る
            for i, box in enumerate(green_by_role[role]):
                display = _display_crop(img_orig, box.bbox, scale)
                raw, frags = ocr.recognize_with_parts(display)
                print(f"  [box#{i} row={box.row_index} col={box.col_index}]")
                print(f"    raw combined: '{raw}'")
                print(f"    fragments ({len(frags)}): {frags}")
                # 従来（連結のみ）と multi の結果を比較
                old_cands = ocr.match_to_green_factor(raw, top_k=5)
                new_cands = ocr.match_to_green_factor_multi(raw, frags, top_k=5)
                print("    old top5 (連結のみ):")
                for name, score in old_cands:
                    mark = " <== 正解" if name == correct else ""
                    print(f"      {score:.3f}  {name}{mark}")
                print("    new top5 (連結+断片):")
                for name, score in new_cands:
                    mark = " <== 正解" if name == correct else ""
                    print(f"      {score:.3f}  {name}{mark}")


if __name__ == "__main__":
    main()

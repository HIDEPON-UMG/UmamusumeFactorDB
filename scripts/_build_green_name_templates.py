"""expected_labels の緑因子名に対応する「タイル左〜中央」（名前領域）crop を集める。

出力: datasets/green_name_templates/<label>/<image>__<role>.png
各画像は 128×16（タイル幅 -★領域 の左 85%）に正規化。
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
OUT_DIR = Path("datasets") / "green_name_templates"
TEMPLATE_SIZE = (128, 16)

ROLES = ["main", "parent1", "parent2"]


def _imwrite_ja(path: Path, img):
    ok, buf = cv2.imencode(".png", img)
    if ok:
        path.write_bytes(buf.tobytes())


def _safe_dirname(label: str) -> str:
    """Windows で使えない文字をエスケープしてディレクトリ名に。"""
    # ファイル名 / ディレクトリ名禁則文字: \\ / : * ? " < > |
    bad = '\\/:*?"<>|'
    return "".join("_" if c in bad else c for c in label)


def _crop_green_name(img_orig, norm, scale, boxes, uma_idx: int):
    """緑 box の左 85%（名前領域）を切り出す。"""
    candidates = [b for b in boxes if b.uma_index == uma_idx and b.col_index == 0 and b.color == "green"]
    if not candidates:
        candidates = [b for b in boxes if b.uma_index == uma_idx and b.row_index == 1 and b.col_index == 0]
    if not candidates:
        return None
    b = candidates[0]
    x0, y0, x1, y1 = b.bbox
    # 名前領域: タイル左 85%
    name_x1 = x0 + int((x1 - x0) * 0.85)
    return _display_crop_from_original(img_orig, (x0, y0, name_x1, y1), scale, pad_y_norm=2)


def main() -> int:
    with EXPECTED.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    saved = 0
    per_label: dict[str, int] = {}
    per_image_cache: dict[str, tuple] = {}

    for r in rows:
        image = r["image_name"]
        role = r["role"]
        uma_idx = ROLES.index(role)
        green_name = r["green_name"]
        if not green_name:
            continue

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

        crop = _crop_green_name(img_orig, norm, scale, boxes, uma_idx)
        if crop is None or crop.size == 0:
            continue
        resized = cv2.resize(crop, TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
        out = OUT_DIR / _safe_dirname(green_name)
        out.mkdir(parents=True, exist_ok=True)
        _imwrite_ja(out / f"{image.replace('.png','')}__{role}.png", resized)
        saved += 1
        per_label[green_name] = per_label.get(green_name, 0) + 1

    # safe→original のマップを出力（templates.py が読む）
    map_path = OUT_DIR / "_label_map.csv"
    import csv as _csv
    with map_path.open("w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["safe_name", "original_name"])
        for label in sorted(per_label.keys()):
            w.writerow([_safe_dirname(label), label])

    print(f"保存: {saved} 枚 / ラベル種: {len(per_label)}")
    print(f"safe→original マップ: {map_path}")
    # サンプル数少ないラベル一覧
    low_count = sorted([(l, c) for l, c in per_label.items() if c <= 1], key=lambda x: x[0])
    print(f"\nサンプル 1 枚のラベル: {len(low_count)}")
    for l, c in low_count[:10]:
        print(f"  {l}: {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

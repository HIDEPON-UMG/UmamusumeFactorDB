"""緑タイル内の★ピクセルの HSV 値を直接プローブする。

指定画像の緑因子 box 領域を切り出し、empty-mask 領域の中心付近の HSV 値を列挙する。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from umafactor.cropper import (
    detect_chara_sections, extract_factor_boxes, normalize_width,
    BASE_WIDTH,
)


def main(image_rel: str) -> int:
    img_orig = cv2.imdecode(
        np.fromfile(image_rel, dtype=np.uint8), cv2.IMREAD_COLOR,
    )
    norm, scale = normalize_width(img_orig, BASE_WIDTH)
    sections = detect_chara_sections(norm)
    boxes = extract_factor_boxes(norm, sections)

    for b in boxes:
        if b.color != "green" or b.row_index != 1:
            continue
        x0, y0, x1, y1 = b.bbox
        right_start = x0 + int((x1 - x0) * 0.4)
        tile = norm[y0:y1, right_start:x1]
        print(f"\n# uma {b.uma_index} row {b.row_index} col {b.col_index} bbox ({x0},{y0},{x1},{y1})")
        print(f"  tile shape: {tile.shape}")
        if tile.size == 0:
            continue
        # タイル右半分を保存
        out = f"debug_green_tile_uma{b.uma_index}.png"
        cv2.imwrite(out, cv2.resize(tile, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
        print(f"  saved: {out}")

        # HSV チャンネルの統計
        hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0]
        s = hsv[:, :, 1]
        v = hsv[:, :, 2]
        # ★の色は多分 中-高彩度 かつ 中-高明度 の非背景色
        # 背景色（緑）を除外して色分布を見る
        # 緑背景の H はおおよそ 40-80、S 中〜低、V 高め
        bg_mask = ((h >= 35) & (h <= 85) & (s < 180))  # 薄緑背景
        non_bg = ~bg_mask
        non_bg_h = h[non_bg]
        non_bg_s = s[non_bg]
        non_bg_v = v[non_bg]
        print(f"  非背景ピクセル {non_bg.sum()} / {tile.shape[0]*tile.shape[1]}")
        if non_bg.any():
            print(f"  H: min={non_bg_h.min()}, max={non_bg_h.max()}, mean={non_bg_h.mean():.0f}")
            print(f"  S: min={non_bg_s.min()}, max={non_bg_s.max()}, mean={non_bg_s.mean():.0f}")
            print(f"  V: min={non_bg_v.min()}, max={non_bg_v.max()}, mean={non_bg_v.mean():.0f}")

        # 画像左上から縦ラインで BGR / HSV をサンプル
        print(f"  横ラインサンプル (中央 y={tile.shape[0]//2}):")
        y_mid = tile.shape[0] // 2
        for x in range(0, tile.shape[1], 5):
            b_, g_, r_ = tile[y_mid, x]
            hv, sv, vv = hsv[y_mid, x]
            print(f"    x={x:2d} BGR=({b_:3d},{g_:3d},{r_:3d}) HSV=({hv:3d},{sv:3d},{vv:3d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/umamusume_20260424_181852_warn.png"))

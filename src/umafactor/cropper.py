"""画像から 3 体のウマ娘セクションを検出し、各因子ボックスを切り出す。

検出戦略（2 段）：
1. 左端バンドの低彩度連続区間から 3 セクションの「おおよその因子グリッド範囲」を得る
2. 各セクション内で、因子ボックス列の「行」を `std > 閾値` の連続ブロックで検出する
   - 単一の因子行は内部でチップ/テキスト/★が std に強弱を出すので、隣接ブロックを
     gap<=5px でマージしてから h が妥当（12-40px）な run を 1 行として採用
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from .config import FactorLayout, FACTOR_COLOR_HSV_RANGES


BASE_WIDTH = 540
BASE_HEIGHT = 960

LOW_SAT_THRESHOLD = 15.0
MIN_GRID_RUN_LEN = 200
SELF_HEADER_HEIGHT = 480
PARENT_HEADER_HEIGHT = 130

# 行検出パラメータ
ROW_CONTENT_STD_THRESHOLD = 15.0  # std がこれを超える行は content
ROW_MERGE_GAP = 4  # 隣接 content run をマージする許容 gap (px)。行間は通常 13px 以上
MIN_ROW_HEIGHT = 24  # 有効な因子行の最小高さ（継承元バナー h=22 を除外する値）
TARGET_ROW_PITCH = 42  # 因子行の実測ピッチ（px）。merge 後の分割に使う
SPLIT_THRESHOLD = 50  # merge 後の高さがこれを超えたら、複数行がまとまった結果として分割
# Row 0（青/赤因子）は低彩度 run 検出範囲より上に位置することがあるので、
# 各セクションで lookback 分だけ上へスキャンを広げる
# 本人は適性バッジなど UI 要素まで距離があるため大きめ、parent は banner 直下で小さめ
SELF_ROW0_LOOKBACK = 90
# parent は factor_y_start より上にも Row 0 が延びることがある。
# ただし名前/評価テキスト領域までは拾わないよう、banner 近傍のみ許容する
PARENT_ROW0_LOOKBACK = 70

FactorColor = Literal["blue", "red", "green", "white"]


@dataclass
class FactorBox:
    uma_index: int
    row_index: int
    col_index: int
    color: FactorColor
    text_img: np.ndarray
    rank_img: np.ndarray
    bbox: tuple[int, int, int, int]


@dataclass
class CharaSection:
    uma_index: int
    factor_y_start: int
    factor_y_end: int
    portrait_bbox: tuple[int, int, int, int]


def normalize_width(img: np.ndarray, target_width: int = BASE_WIDTH) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    if w == target_width:
        return img, 1.0
    scale = target_width / w
    new_h = int(round(h * scale))
    return cv2.resize(img, (target_width, new_h), interpolation=cv2.INTER_AREA), scale


def _row_saturation(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    band = img[:, 0 : int(w * 0.15)]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    return hsv[:, :, 1].mean(axis=1)


def _find_low_sat_runs(row_sat: np.ndarray, threshold: float, min_len: int) -> list[tuple[int, int]]:
    mask = row_sat < threshold
    runs: list[tuple[int, int]] = []
    start = 0
    in_run = False
    for y, flag in enumerate(mask):
        if flag and not in_run:
            start = y
            in_run = True
        elif not flag and in_run:
            if y - start >= min_len:
                runs.append((start, y))
            in_run = False
    if in_run and len(mask) - start >= min_len:
        runs.append((start, len(mask)))
    return runs


def detect_chara_sections(img: np.ndarray) -> list[CharaSection]:
    h = img.shape[0]
    row_sat = _row_saturation(img)
    runs = _find_low_sat_runs(row_sat, LOW_SAT_THRESHOLD, MIN_GRID_RUN_LEN)
    runs_by_len = sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:3]
    grids = sorted(runs_by_len, key=lambda r: r[0])
    if len(grids) != 3:
        raise RuntimeError(f"因子グリッドを 3 領域検出できませんでした（{len(grids)} 件）")

    sections: list[CharaSection] = []
    for i, (g_start, g_end) in enumerate(grids):
        header_h = SELF_HEADER_HEIGHT if i == 0 else PARENT_HEADER_HEIGHT
        portrait_y0 = max(0, g_start - header_h)
        portrait_y1 = max(portrait_y0 + 10, g_start - 10)
        w = img.shape[1]
        sections.append(
            CharaSection(
                uma_index=i,
                factor_y_start=g_start,
                factor_y_end=g_end,
                portrait_bbox=(int(w * 0.01), portrait_y0, int(w * 0.18), portrait_y1),
            )
        )
    return sections


def _detect_factor_rows(
    img: np.ndarray, section: CharaSection, layout: FactorLayout
) -> list[tuple[int, int]]:
    """セクションの因子行を (y_top, y_bottom) のリストで返す。

    uma0（本人）のみ、低彩度 run より上にある Row 0 を拾うため y 下限を広げる。
    """
    w = img.shape[1]
    left_x0 = int(round(w * layout.left_x0))
    left_x1 = int(round(w * layout.left_x1))

    lookback = SELF_ROW0_LOOKBACK if section.uma_index == 0 else PARENT_ROW0_LOOKBACK
    y_start = max(0, section.factor_y_start - lookback)
    y_end = min(img.shape[0], section.factor_y_end + 5)

    stds = np.array([
        img[y, left_x0:left_x1].std() for y in range(y_start, y_end)
    ])
    mask = stds > ROW_CONTENT_STD_THRESHOLD

    # content runs を抽出
    raw_runs: list[tuple[int, int]] = []
    s = 0
    in_r = False
    for i, f in enumerate(mask):
        if f and not in_r:
            s = i
            in_r = True
        elif not f and in_r:
            if i - s >= 3:
                raw_runs.append((y_start + s, y_start + i))
            in_r = False
    if in_r and len(mask) - s >= 3:
        raw_runs.append((y_start + s, y_start + len(mask)))

    # gap <= ROW_MERGE_GAP の隣接 run をマージ
    merged: list[tuple[int, int]] = []
    for a, b in raw_runs:
        if merged and a - merged[-1][1] <= ROW_MERGE_GAP:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    # h > SPLIT_THRESHOLD のブロックは複数行が融合した結果なので、等分割
    split_rows: list[tuple[int, int]] = []
    for a, b in merged:
        h = b - a
        if h > SPLIT_THRESHOLD:
            n = max(2, round(h / TARGET_ROW_PITCH))
            piece = h // n
            for i in range(n - 1):
                split_rows.append((a + i * piece, a + (i + 1) * piece))
            split_rows.append((a + (n - 1) * piece, b))
        else:
            split_rows.append((a, b))

    # 高さ下限フィルタ（バナー h=22 を除外）
    rows = [(a, b) for a, b in split_rows if (b - a) >= MIN_ROW_HEIGHT]
    return rows


def detect_factor_color(box_bgr: np.ndarray) -> FactorColor:
    """因子ボックスの左端色チップから青/赤/緑/白を判定する。"""
    h, w = box_bgr.shape[:2]
    chip = box_bgr[:, 0 : max(4, int(w * 0.15))]
    hsv = cv2.cvtColor(chip, cv2.COLOR_BGR2HSV)

    def ratio_in_range(lo, hi) -> float:
        mask = cv2.inRange(hsv, np.array(lo, dtype=np.uint8), np.array(hi, dtype=np.uint8))
        return float(mask.mean()) / 255.0

    scores = {
        "blue": ratio_in_range(*FACTOR_COLOR_HSV_RANGES["blue"]),
        "green": ratio_in_range(*FACTOR_COLOR_HSV_RANGES["green"]),
        "red": ratio_in_range(*FACTOR_COLOR_HSV_RANGES["red"]),
    }
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if scores[best] > 0.25:
        return best  # type: ignore[return-value]
    return "white"


def extract_factor_boxes(
    img: np.ndarray,
    sections: list[CharaSection],
    layout: FactorLayout | None = None,
) -> list[FactorBox]:
    """各セクションの因子行を検出して左右 2 列のボックスを切り出す。

    umacapture の実測に合わせ、因子ボックスは行 top から 27 px 高で固定クロップする
    （recognizer.json の box_height_rel = 0.0278 * 960 = 27）。
    rank 領域はボックス内 y=11..27（下部 16 px）、x=48..99（0.29..0.59 相対）。
    """
    layout = layout or FactorLayout()
    w = img.shape[1]

    left_x0 = int(round(w * layout.left_x0))
    left_x1 = int(round(w * layout.left_x1))
    right_x0 = int(round(w * layout.right_x0))
    right_x1 = int(round(w * layout.right_x1))

    # 固定クロップサイズ（umacapture 準拠）
    box_h = 27
    # rank クロップ: 因子ボックス内の下部領域（★が配置される場所）
    rank_y_offset = 11  # ボックス top からの y オフセット
    rank_h = 16
    rank_x_offset = 48  # ボックス left からの x オフセット（560x960 基準で 0.0889×540）
    rank_w = 52

    boxes: list[FactorBox] = []
    for section in sections:
        rows = _detect_factor_rows(img, section, layout)
        for row_idx, (y_top, _y_bot) in enumerate(rows):
            box_y1 = min(img.shape[0], y_top + box_h)
            for col_idx, (xa, xb) in enumerate([(left_x0, left_x1), (right_x0, right_x1)]):
                box_bgr = img[y_top:box_y1, xa:xb]
                if box_bgr.size == 0 or _is_blank_row(box_bgr):
                    continue
                color = detect_factor_color(box_bgr)
                text_img = cv2.resize(box_bgr, (168, 16), interpolation=cv2.INTER_AREA)

                rank_y0 = y_top + rank_y_offset
                rank_y1 = min(img.shape[0], rank_y0 + rank_h)
                rank_x0 = xa + rank_x_offset
                rank_x1 = min(img.shape[1], rank_x0 + rank_w)
                rank_raw = img[rank_y0:rank_y1, rank_x0:rank_x1]
                if rank_raw.size == 0:
                    continue
                rank_img = cv2.resize(rank_raw, (52, 16), interpolation=cv2.INTER_AREA)

                boxes.append(
                    FactorBox(
                        uma_index=section.uma_index,
                        row_index=row_idx,
                        col_index=col_idx,
                        color=color,
                        text_img=text_img,
                        rank_img=rank_img,
                        bbox=(xa, y_top, xb, box_y1),
                    )
                )
    return boxes


def _is_blank_row(box_bgr: np.ndarray) -> bool:
    gray = cv2.cvtColor(box_bgr, cv2.COLOR_BGR2GRAY)
    return bool(gray.std() < 10.0)


def _crop_rank_region(box_bgr: np.ndarray, layout: FactorLayout) -> np.ndarray:
    h, w = box_bgr.shape[:2]
    x0 = int(round(w * layout.rank_x0_in_box_rel))
    x1 = int(round(w * layout.rank_x1_in_box_rel))
    x0 = max(0, min(x0, w - 1))
    x1 = max(x0 + 1, min(x1, w))
    rank_region = box_bgr[:, x0:x1]
    return cv2.resize(rank_region, (52, 16), interpolation=cv2.INTER_AREA)

"""画像から 3 体のウマ娘セクションを検出し、各因子ボックスを切り出す。

検出戦略：

セクション検出（detect_chara_sections）：
  左端バンドの低彩度連続区間から 3 セクションの「おおよその因子グリッド範囲」を得る。

因子ボックス検出（extract_factor_boxes）：
  v2 = ★検出駆動（新経路、既定）：
    1. 画像全体から黄色の埋まった★を HSV マスク + 連結成分で抽出
    2. y 近接で行クラスタ化、x で左右列に振り分け
    3. 左列/右列それぞれの★ x 最大値から「タイル右端」を推定（layout 比率には非依存）
    4. タイル幅は固定値（TILE_WIDTH）で左端を算出、bbox は★中心 y を基準に構成
    ★が検出できた行のみ因子行として採用。★0 個（全て空）のケースは既知の限界。

  legacy = 低彩度+std ベース（fallback、UmamusumeReceiptMaker 非経由の
  ゲーム直撮り画像で★検出が失敗した場合に使用）：
    各セクション内で std > 閾値の連続ブロックを因子行として検出し、
    layout.left_x0 等の比率で左右列 x 範囲を決める。
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

# === ★検出（新経路）パラメータ =========================================
# 金★（埋まっている★）を HSV で拾う範囲。空★（薄ピンク縁）は閾値外で除外。
STAR_HSV_LO = (15, 120, 180)
STAR_HSV_HI = (40, 255, 255)
# ★連結成分のサイズ制限（正規化幅 540 基準）
STAR_MIN_W = 5
STAR_MAX_W = 25
STAR_MIN_H = 5
STAR_MAX_H = 25
STAR_MIN_AREA = 15
STAR_MAX_AREA = 400
# 同一行とみなす y 許容（★の中心間距離）
# 行ピッチは ~45px、★高さは ~15px。閾値を小さくし過ぎると、
# 単一因子行の★でも y がわずかにバラつくと 2 行に分裂してしまう
# （アド・アストラ 1 行が分裂して次行を誤検出するケースで顕在化）。
STAR_ROW_Y_TOL = 12
# 新経路の因子タイル寸法（正規化幅 540 基準）
# 実測：チップ(x=88付近) 〜 ★パディング右端(x=260付近) で幅 ≒ 175px
TILE_WIDTH = 175
TILE_HEIGHT = 27  # 旧 box_h=27 を踏襲（pipeline._crop_rank_from_original の y オフセットと整合）
# ★中心が bbox 内でこの y オフセットに位置するよう bbox.y0 を決める
# 旧 rank 領域 y=11..27 の中央 y=19 と一致させる
STAR_Y_IN_TILE = 19
# ★最右端からタイル右端までの余白（実測で ★右端 x=224 → タイル右端 x=260 程度）
TILE_RIGHT_PADDING = 36
# タイル右端推定に使う★最右端分布の percentile（★3個行の値を基準にする）
TILE_RIGHT_PERCENTILE = 90
# 新経路で必要な最小行数（これ未満なら legacy にフォールバック）
MIN_DETECTED_ROWS = 3
# タイル左端推定に使う★のサンプル数（少なすぎるとノイズに弱い）
MIN_STARS_PER_COLUMN = 3

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
    # ★検出が効いた新経路で、実★クラスタの正規化座標 (x0, y0, x1, y1) を保持する。
    # None の場合（legacy 経路）は pipeline 側で layout.rank_x0_in_box_rel から計算。
    rank_bbox: tuple[int, int, int, int] | None = None


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
        # 低彩度 run 検出が 3 セクション分取れなかった場合は、★検出クラスタの y
        # 分布から 3 セクションを推定する fallback を試す。
        fallback = _detect_chara_sections_by_stars(img)
        if fallback is not None:
            return fallback
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


def _detect_chara_sections_by_stars(img: np.ndarray) -> list[CharaSection] | None:
    """★検出クラスタの y 分布から 3 セクションを推定する fallback。

    行ピッチ (~45px) を基準に ★行を密集群としてグループ化し、
    偽陽性（ヘッダー装飾等）を除外したうえで 3 セクションを取り出す。
    - gap > SECTION_SPLIT_GAP (= 65px) で群を分割
    - 群の中で行数 >= MIN_ROWS_PER_SECTION (= 5) かつ 長さ >= MIN_SECTION_SPAN (= 100px)
      のものだけを因子欄セクション候補とみなす
    - 3 つ以上取れたら y 順に先頭 3 つを採用、取れなければ None（legacy を素通り）
    """
    SECTION_SPLIT_GAP = 65
    MIN_ROWS_PER_SECTION = 5
    MIN_SECTION_SPAN = 100

    stars = _detect_golden_stars(img)
    classified = _cluster_stars_into_rows(stars, img.shape[1])
    if len(classified) < MIN_ROWS_PER_SECTION * 3:
        return None
    ys = sorted(r[0] for r in classified)

    groups: list[list[int]] = [[ys[0]]]
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] > SECTION_SPLIT_GAP:
            groups.append([ys[i]])
        else:
            groups[-1].append(ys[i])

    big_groups = [
        g for g in groups
        if len(g) >= MIN_ROWS_PER_SECTION and (g[-1] - g[0]) >= MIN_SECTION_SPAN
    ]
    if len(big_groups) < 3:
        return None

    sections_y = sorted(big_groups, key=lambda g: g[0])[:3]
    w = img.shape[1]
    sections: list[CharaSection] = []
    for i, grp in enumerate(sections_y):
        y_s, y_e = grp[0], grp[-1]
        header_h = SELF_HEADER_HEIGHT if i == 0 else PARENT_HEADER_HEIGHT
        portrait_y0 = max(0, y_s - header_h)
        portrait_y1 = max(portrait_y0 + 10, y_s - 10)
        sections.append(
            CharaSection(
                uma_index=i,
                factor_y_start=y_s,
                factor_y_end=y_e,
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


def _detect_golden_stars(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """画像全体から金★（埋まっている★）候補を (x, y, w, h) で返す。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(STAR_HSV_LO, dtype=np.uint8), np.array(STAR_HSV_HI, dtype=np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    stars: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if (
            STAR_MIN_W <= w <= STAR_MAX_W
            and STAR_MIN_H <= h <= STAR_MAX_H
            and STAR_MIN_AREA <= area <= STAR_MAX_AREA
        ):
            stars.append((int(x), int(y), int(w), int(h)))
    return stars


def _cluster_stars_into_rows(
    stars: list[tuple[int, int, int, int]],
    img_width: int,
) -> list[tuple[int, list[tuple[int, int, int, int]], list[tuple[int, int, int, int]]]]:
    """★候補を y 近接でクラスタ化し、画像中央 x で左右列に振り分ける。

    Returns:
        [(y_center, left_stars, right_stars), ...]（y で昇順）
    """
    if not stars:
        return []
    mid_x = img_width // 2
    stars_sorted = sorted(stars, key=lambda s: s[1] + s[3] // 2)
    rows: list[list[tuple[int, int, int, int]]] = []
    for s in stars_sorted:
        cy = s[1] + s[3] // 2
        if rows:
            ref_cy = int(np.mean([r[1] + r[3] // 2 for r in rows[-1]]))
            if abs(cy - ref_cy) <= STAR_ROW_Y_TOL:
                rows[-1].append(s)
                continue
        rows.append([s])

    classified: list[tuple[int, list[tuple[int, int, int, int]], list[tuple[int, int, int, int]]]] = []
    for row in rows:
        left = [s for s in row if s[0] + s[2] // 2 < mid_x]
        right = [s for s in row if s[0] + s[2] // 2 >= mid_x]
        if not left and not right:
            continue
        y_center = int(np.mean([s[1] + s[3] // 2 for s in row]))
        classified.append((y_center, left, right))
    return classified


def _estimate_tile_right_edges(
    classified_rows: list[tuple[int, list[tuple[int, int, int, int]], list[tuple[int, int, int, int]]]],
) -> tuple[int | None, int | None]:
    """左列/右列それぞれの「タイル右端 x」を★の x_right 中央値から推定。

    ★は常にタイル右端近くに並ぶので、★の最右端 + 数 px がタイル右端に相当する。
    """
    left_maxes: list[int] = []
    right_maxes: list[int] = []
    for _y, left, right in classified_rows:
        if left:
            left_maxes.append(max(s[0] + s[2] for s in left))
        if right:
            right_maxes.append(max(s[0] + s[2] for s in right))
    # ★は行ごとに 1-3 個と可変なので、中央値では★1個行に引っ張られる。
    # ★3個行の★右端を基準にするため上位 percentile を使う。
    # 更に ★右端の右側にあるタイル余白（TILE_RIGHT_PADDING）を足してタイル右端とする。
    x_L1 = (
        int(np.percentile(left_maxes, TILE_RIGHT_PERCENTILE)) + TILE_RIGHT_PADDING
        if len(left_maxes) >= MIN_STARS_PER_COLUMN
        else None
    )
    x_R1 = (
        int(np.percentile(right_maxes, TILE_RIGHT_PERCENTILE)) + TILE_RIGHT_PADDING
        if len(right_maxes) >= MIN_STARS_PER_COLUMN
        else None
    )
    return x_L1, x_R1


def _assign_row_to_section(
    row_y: int, sections: list[CharaSection]
) -> int | None:
    """因子行 y がどの CharaSection に属するかを判定。

    セクションの y 範囲（lookback 込み）に入る最初のセクション番号を返す。
    どこにも入らなければ None。
    """
    for sec in sections:
        lookback = SELF_ROW0_LOOKBACK if sec.uma_index == 0 else PARENT_ROW0_LOOKBACK
        y_start = max(0, sec.factor_y_start - lookback)
        y_end = sec.factor_y_end + 10
        if y_start <= row_y <= y_end:
            return sec.uma_index
    return None




def extract_factor_boxes(
    img: np.ndarray,
    sections: list[CharaSection],
    layout: FactorLayout | None = None,
) -> list[FactorBox]:
    """因子ボックスを ★検出駆動で抽出（新経路）。失敗時は legacy にフォールバック。

    新経路は UmamusumeReceiptMaker の合成画像や解像度の異なるスクショでも
    layout 比率に依存せず動作する。旧経路は legacy 関数として保存している。
    """
    layout = layout or FactorLayout()
    stars = _detect_golden_stars(img)
    classified_rows = _cluster_stars_into_rows(stars, img.shape[1])

    if len(classified_rows) < MIN_DETECTED_ROWS:
        return _extract_factor_boxes_legacy(img, sections, layout)

    x_L1, x_R1 = _estimate_tile_right_edges(classified_rows)
    if x_L1 is None or x_R1 is None:
        return _extract_factor_boxes_legacy(img, sections, layout)

    x_L0 = max(0, x_L1 - TILE_WIDTH)
    x_R0 = max(0, x_R1 - TILE_WIDTH)

    # row_index はセクション内で 0 から数える
    per_section_row_idx: dict[int, int] = {}
    boxes: list[FactorBox] = []

    for y_center, left_stars, right_stars in classified_rows:
        uma_idx = _assign_row_to_section(y_center, sections)
        if uma_idx is None:
            continue
        row_idx = per_section_row_idx.get(uma_idx, 0)
        per_section_row_idx[uma_idx] = row_idx + 1
        boxes.extend(
            _build_boxes_for_row(
                img, uma_idx, row_idx, y_center, left_stars, right_stars,
                x_L0, x_L1, x_R0, x_R1,
            )
        )
    if len(boxes) < MIN_DETECTED_ROWS:
        return _extract_factor_boxes_legacy(img, sections, layout)
    return boxes


def _build_boxes_for_row(
    img: np.ndarray,
    uma_idx: int,
    row_idx: int,
    y_center: int,
    left_stars: list,
    right_stars: list,
    x_L0: int,
    x_L1: int,
    x_R0: int,
    x_R1: int,
) -> list[FactorBox]:
    """1 つの★行について左右列の FactorBox を生成する（等間隔チェーン抽出後に呼ぶ）。"""
    boxes: list[FactorBox] = []
    # ★中心 y が bbox 内 y=STAR_Y_IN_TILE に来るよう bbox y0 を決める
    y_top = max(0, y_center - STAR_Y_IN_TILE)
    y_bot = min(img.shape[0], y_top + TILE_HEIGHT)
    if y_bot - y_top < TILE_HEIGHT // 2:
        return boxes

    for col_idx, (xa, xb, col_stars) in enumerate(
        [(x_L0, x_L1, left_stars), (x_R0, x_R1, right_stars)]
    ):
        # row 0 は各ウマ娘の青(col=0)/赤(col=1)スロット。このペアが常に存在する
        # ゲーム UI 構造を使い、★が全て空のケース（金★0 で検出できない）でも
        # row 0 に限って bbox を作る（位置ベース救済で pipeline 側が赤/青に割り当てる）。
        # row >= 1 はノイズを拾わないよう従来通り★未検出はスキップ。
        if not col_stars and row_idx > 0:
            continue
        xa_c = max(0, xa)
        xb_c = min(img.shape[1], xb)
        box_bgr = img[y_top:y_bot, xa_c:xb_c]
        if box_bgr.size == 0 or _is_blank_row(box_bgr):
            continue
        color = detect_factor_color(box_bgr)
        text_img = cv2.resize(box_bgr, (168, 16), interpolation=cv2.INTER_AREA)

        # 緑因子タイルは左端の黄色アイコン（固有スキル UI 装飾）が金★マスクに
        # 偽陽性として引っかかるため、bbox の右 60% に位置する★のみを採用する。
        # （青/赤タイルは左端アイコンが青/赤系で金★マスクに引っかからないので除外不要）
        effective_stars = col_stars
        if color == "green" and col_stars:
            x_threshold = xa_c + int((xb_c - xa_c) * 0.4)
            effective_stars = [
                s for s in col_stars if (s[0] + s[2] // 2) >= x_threshold
            ]

        if effective_stars:
            # rank モデルは「★3スロット全体の金色面積比」でランクを判定するため、
            # 金★のみの狭い crop だと画像がほぼ金色一色になり ★3 と誤判定しやすい。
            # 空★（未点灯★）を取り込むよう、★3 スロット相当の幅を右側に確保する。
            # （金★は左→右の順で点灯、残りは右側の空★なので、右拡張で自然に空★を含む）
            sr_x0 = min(s[0] for s in effective_stars)
            sr_x1 = max(s[0] + s[2] for s in effective_stars)
            MIN_RANK_W = 50  # ★3 スロット ~15px × 3 + 間隔 ≒ 50px
            if sr_x1 - sr_x0 < MIN_RANK_W:
                sr_x1 = min(xb_c, sr_x0 + MIN_RANK_W)
            rx0 = max(0, sr_x0 - 2)
            rx1 = min(img.shape[1], sr_x1 + 2)

            if color == "green":
                # 緑タイルは左端黄色●アイコンで★行の y が上にずれがち。同じ行の
                # 右列★（偽陽性が少なく信頼できる）の中心 y から rank y を再計算。
                if right_stars:
                    right_y = int(np.mean([s[1] + s[3] // 2 for s in right_stars]))
                    ry0 = max(0, right_y - 8)
                    ry1 = min(img.shape[0], right_y + 8)
                else:
                    ry0 = y_top + 11
                    ry1 = min(img.shape[0], y_top + 27)
            else:
                ry0 = max(0, min(s[1] for s in effective_stars) - 2)
                ry1 = min(img.shape[0], max(s[1] + s[3] for s in effective_stars) + 2)
            rank_bbox: tuple[int, int, int, int] | None = (rx0, ry0, rx1, ry1)
        else:
            # ★未検出（全て空★、または緑因子で右 60% に★無し）:
            # bbox 右端の layout 比率から rank 領域を推定
            rel_x0 = 0.6786  # = FactorLayout.rank_x0_in_box_rel
            box_w = xb_c - xa_c
            rx0 = xa_c + int(round(box_w * rel_x0))
            rx1 = xb_c
            ry0 = y_top + 11
            ry1 = min(img.shape[0], y_top + 27)
            rank_bbox = None  # pipeline は bbox の layout 比率で再計算できるよう明示的に None

        rank_raw = img[ry0:ry1, rx0:rx1]
        if rank_raw.size == 0:
            continue
        rank_img = cv2.resize(rank_raw, (52, 16), interpolation=cv2.INTER_AREA)

        boxes.append(
            FactorBox(
                uma_index=uma_idx,
                row_index=row_idx,
                col_index=col_idx,
                color=color,
                text_img=text_img,
                rank_img=rank_img,
                bbox=(xa_c, y_top, xb_c, y_bot),
                rank_bbox=rank_bbox,
            )
        )
    return boxes


def _extract_factor_boxes_legacy(
    img: np.ndarray,
    sections: list[CharaSection],
    layout: FactorLayout,
) -> list[FactorBox]:
    """layout 比率依存の旧ロジック（ゲーム直撮り画像の fallback 用）。

    umacapture の実測に合わせ、因子ボックスは行 top から 27 px 高で固定クロップする
    （recognizer.json の box_height_rel = 0.0278 * 960 = 27）。
    rank 領域はボックス内 y=11..27（下部 16 px）、x=48..99（0.29..0.59 相対）。
    """
    w = img.shape[1]

    left_x0 = int(round(w * layout.left_x0))
    left_x1 = int(round(w * layout.left_x1))
    right_x0 = int(round(w * layout.right_x0))
    right_x1 = int(round(w * layout.right_x1))

    box_h = 27
    rank_y_offset = 11
    rank_h = 16

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

                box_w = xb - xa
                rank_y0 = y_top + rank_y_offset
                rank_y1 = min(img.shape[0], rank_y0 + rank_h)
                rank_x0 = xa + int(round(box_w * layout.rank_x0_in_box_rel))
                rank_x1 = min(img.shape[1], xa + int(round(box_w * layout.rank_x1_in_box_rel)))
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

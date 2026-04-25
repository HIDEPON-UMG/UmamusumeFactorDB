"""画像 → (Submission, ReviewQueue) への統合パイプライン。

ReviewQueue は低信頼度の因子をユーザにレビューしてもらうための候補リスト。
"""

from __future__ import annotations

import cv2
import numpy as np

from .config import load_unique_skill_to_character
from .cropper import (
    BASE_WIDTH,
    CharaSection,
    FactorBox,
    detect_chara_sections,
    extract_factor_boxes,
    normalize_width,
)
from .infer import get_predictor
from .ocr import get_ocr
from .review import ReviewItem, ReviewQueue
from .schema import FactorEntry, Submission, UmaFactors
from .templates import match_green_name, match_green_star, match_star, match_templates


BLUE_FACTOR_TYPES = ["スピード", "スタミナ", "パワー", "根性", "賢さ"]
RED_FACTOR_TYPES = [
    "芝", "ダート",
    "短距離", "マイル", "中距離", "長距離",
    "逃げ", "先行", "差し", "追込",
]

# 青因子向けの軽い摂動セット
PERTURBATIONS_BLUE: list[tuple[int, int]] = [
    (dy, dx) for dy in range(-2, 3) for dx in range(-1, 2)
]

# 赤因子向けの大きい摂動セット（短/中/長距離の字形差を吸収）
PERTURBATIONS_RED: list[tuple[int, int]] = [
    (dy, dx) for dy in range(-5, 6) for dx in range(-3, 4)
]

# ★数（rank）向けの軽量摂動（9 パターン）
# rank モデルは softmax 出力を持たないため、infer.predict_with_perturbation で
# 各シフト画像の argmax+confidence を集計し、confidence 加算投票で判定する。
PERTURBATIONS_RANK: list[tuple[int, int]] = [
    (dy, dx) for dy in range(-1, 2) for dx in range(-1, 2)
]

UMA_ROLES = ["main", "parent1", "parent2"]


def _merge_candidates(
    onnx_cands: list[tuple[str, float]],
    ocr_cands: list[tuple[str, float]],
    limit: int = 8,
    ocr_weight: float = 1.25,
    ocr_strong_threshold: float = 0.7,
    onnx_weight: float = 1.0,
    both_bonus: float = 0.15,
) -> tuple[list[tuple[str, float]], dict[str, str]]:
    """ONNX と OCR の候補を統合スコアでマージする。

    Uma のゲームフォントに対しては EasyOCR の方が ONNX より圧倒的に正確なので、
    OCR 由来候補に重みをかける：

    - 同名が両方にある → スコア = max(ONNX, OCR) + both_bonus （両者一致は信頼度高）
    - OCR のみ → スコア = OCR × ocr_weight（OCR を優遇）
    - ONNX のみ → スコア = ONNX × onnx_weight

    さらに OCR の top1 が ocr_strong_threshold 以上なら OCR top1 を強制的に最優先。
    """
    onnx_map = {n: s for n, s in onnx_cands}
    ocr_map = {n: s for n, s in ocr_cands}

    combined: dict[str, tuple[float, str]] = {}
    for name, s in onnx_cands:
        combined[name] = (s * onnx_weight, "onnx")
    for name, s in ocr_cands:
        if name in combined:
            # 両方にある場合
            prev_score = combined[name][0]
            new_score = max(prev_score, s * ocr_weight) + both_bonus
            combined[name] = (new_score, "both")
        else:
            combined[name] = (s * ocr_weight, "ocr")

    # ソート
    ordered = sorted(combined.items(), key=lambda kv: -kv[1][0])

    # OCR top1 が強い場合、先頭に移動（fuzzy ratio が exact/near-exact）
    if ocr_cands and ocr_cands[0][1] >= ocr_strong_threshold:
        top_ocr_name = ocr_cands[0][0]
        # ordered の中から top_ocr_name を先頭に
        ordered = [(n, v) for n, v in ordered if n == top_ocr_name] + [
            (n, v) for n, v in ordered if n != top_ocr_name
        ]

    sources = {n: v[1] for n, v in ordered}
    merged = [(n, min(1.0, v[0])) for n, v in ordered][:limit]
    return merged, sources


def _extract_character_icon_bgr(img: np.ndarray, section: CharaSection) -> np.ndarray:
    x0, y0, x1, y1 = section.portrait_bbox
    h_sec = y1 - y0
    icon_size = min(h_sec, x1 - x0)
    cy = y0 + icon_size // 2
    cx = x0 + icon_size // 2
    half = icon_size // 2
    crop = img[max(0, cy - half): cy + half, max(0, cx - half): cx + half]
    if crop.size == 0:
        return np.zeros((32, 32, 3), dtype=np.uint8)
    return cv2.resize(crop, (32, 32), interpolation=cv2.INTER_LINEAR)


def _crop_from_original(
    img_orig: np.ndarray,
    bbox: tuple[int, int, int, int],
    scale: float,
    dy: int = 0,
    dx: int = 0,
) -> np.ndarray:
    inv = 1.0 / scale if scale != 0 else 1.0
    x0, y0, x1, y1 = bbox
    ox0 = int(round(x0 * inv)) + dx
    oy0 = int(round(y0 * inv)) + dy
    ox1 = int(round(x1 * inv)) + dx
    oy1 = int(round(y1 * inv)) + dy
    ox0 = max(0, min(ox0, img_orig.shape[1]))
    ox1 = max(ox0 + 1, min(ox1, img_orig.shape[1]))
    oy0 = max(0, min(oy0, img_orig.shape[0]))
    oy1 = max(oy0 + 1, min(oy1, img_orig.shape[0]))
    return img_orig[oy0:oy1, ox0:ox1]


def _display_crop_from_original(
    img_orig: np.ndarray,
    bbox: tuple[int, int, int, int],
    scale: float,
    pad_y_norm: int = 2,
) -> np.ndarray:
    """レビュー UI 表示 + 赤/青 OCR 用の広めクロップ。

    モデル入力領域（recognizer.json の left_rect / right_rect）は因子名テキストの
    左端が欠ける場合があるため、UI で確認しやすいよう左右と上下にパディングを追加する。
    ONNX 推論には使わないが、**OCR には display_crop を使っている**ので、
    pad_y_norm を増やすと赤/青因子の allowlist OCR がテキストを拾える率が上がる。
    """
    inv = 1.0 / scale if scale != 0 else 1.0
    x0, y0, x1, y1 = bbox
    # 正規化 540 基準で 左 +32px、右 +8px、上下は引数（既定 +2px）
    PAD_LEFT_NORM = 32
    PAD_RIGHT_NORM = 8
    ox0 = int(round((x0 - PAD_LEFT_NORM) * inv))
    oy0 = int(round((y0 - pad_y_norm) * inv))
    ox1 = int(round((x1 + PAD_RIGHT_NORM) * inv))
    oy1 = int(round((y1 + pad_y_norm) * inv))
    ox0 = max(0, ox0)
    oy0 = max(0, oy0)
    ox1 = min(img_orig.shape[1], ox1)
    oy1 = min(img_orig.shape[0], oy1)
    return img_orig[oy0:oy1, ox0:ox1]


def _crop_rank_from_original(
    img_orig: np.ndarray,
    bbox: tuple[int, int, int, int],
    scale: float,
    rank_bbox: tuple[int, int, int, int] | None = None,
) -> np.ndarray:
    """因子ボックスの★領域を元解像度から切り出す。

    rank_bbox が与えられた場合（★検出駆動の新経路）はその正規化座標を元解像度に
    投影して切り出す。与えられなかった場合（legacy 経路）は layout.rank_x0_in_box_rel
    に従って bbox 幅の 67.86%〜100% を★領域として取り出す。
    """
    inv = 1.0 / scale if scale != 0 else 1.0

    if rank_bbox is not None:
        rank_x0_norm, rank_y0_norm, rank_x1_norm, rank_y1_norm = rank_bbox
        # 実★クラスタは bbox より狭いので、モデル入力が 52x16 の比率に近づくよう
        # y 方向に若干のパディング（2px）を足して安定化させる
        rank_y0_norm -= 2
        rank_y1_norm += 2
    else:
        x0, y0, x1, y1 = bbox
        box_w_norm = x1 - x0
        rel_x0 = 0.6786  # = FactorLayout.rank_x0_in_box_rel
        rel_x1 = 1.0
        rank_x0_norm = x0 + int(round(box_w_norm * rel_x0))
        rank_x1_norm = x0 + int(round(box_w_norm * rel_x1))
        rank_y0_norm = y0 + 11
        rank_y1_norm = y0 + 27

    rx0 = int(round(rank_x0_norm * inv))
    ry0 = int(round(rank_y0_norm * inv))
    rx1 = int(round(rank_x1_norm * inv))
    ry1 = int(round(rank_y1_norm * inv))
    rx0 = max(0, rx0)
    ry0 = max(0, ry0)
    rx1 = min(img_orig.shape[1], rx1)
    ry1 = min(img_orig.shape[0], ry1)
    return img_orig[ry0:ry1, rx0:rx1]


def analyze_image(
    image_path: str,
    submitter_id: str,
    debug_crops_dir: str | None = None,
) -> tuple[Submission, ReviewQueue]:
    img_orig = cv2.imread(image_path)
    if img_orig is None:
        raise FileNotFoundError(f"画像を読み込めませんでした: {image_path}")

    norm_img, scale = normalize_width(img_orig, BASE_WIDTH)

    sections = detect_chara_sections(norm_img)
    if len(sections) < 3:
        raise RuntimeError(
            f"ウマ娘セクションを 3 体分検出できませんでした（検出数={len(sections)}）"
        )
    boxes = extract_factor_boxes(norm_img, sections)

    if debug_crops_dir:
        _dump_debug_crops(norm_img, sections, boxes, debug_crops_dir)

    factor_pred = get_predictor("factor")
    rank_pred = get_predictor("factor_rank")
    char_pred = get_predictor("character")
    ocr = get_ocr()
    # 緑因子（固有スキル 249 件）の名前セット。非緑スロットの ONNX/OCR 候補から
    # 除外するフィルタに使う。
    green_name_set: set[str] = set(ocr._green_factor_names)

    umas = [UmaFactors(), UmaFactors(), UmaFactors()]
    review = ReviewQueue()
    white_counters = {0: 0, 1: 0, 2: 0}

    for section in sections:
        icon = _extract_character_icon_bgr(norm_img, section)
        pred = char_pred.predict(icon)
        umas[section.uma_index].character = pred.label

    # Pass 0: 各 uma の緑 box 候補について OCR top1 conf と最大 gold_star_count を
    # 別々に事前計算する。従来の「gold_star_count>0 の先着 box を採用」だと、
    # ★全空の行（row=1 col=0、実はテキストが正解）が skip されて row=2 の
    # 別 box（OCR は空や雑音）が採用される事故が多発していた。
    # 因子名は「OCR top1 conf 最大の box」、★は「同 uma 内の緑 box の最大
    # gold_star_count」と別軸で採用することで、rank fallback が誤★を返す問題も回避。
    # 緑候補には UI 仕様上 row=1 col=0（青因子の下）の絶対位置 box も必ず含める。
    # detect_factor_color が緑→white 等に誤判定しても、位置ベースで緑候補に入れる
    # ことで「緑因子が白スキルに流入する」事故を防ぐ。
    best_green_box: dict[int, FactorBox] = {}
    best_green_score: dict[int, float] = {}
    best_green_gold: dict[int, int] = {}  # 名前採用時の最大 gold（col=0 のみ）
    any_green_gold: dict[int, int] = {}  # ★補填用の最大 gold（col 問わず）
    for box in boxes:
        # 緑候補: 色判定 green の box、または UI 仕様上の絶対位置（row=1 col=0）。
        # 位置ベース候補を加えることで、色チップ検出が white 等に失敗しても
        # 緑因子が拾える（ユーザー報告: 緑因子が白因子扱いで混入した）。
        is_green_candidate = (
            box.color == "green"
            or (box.row_index == 1 and box.col_index == 0)
        )
        if not is_green_candidate:
            continue
        g = box.gold_star_count or 0
        if g > any_green_gold.get(box.uma_index, 0):
            any_green_gold[box.uma_index] = g
        # 緑因子は UI 仕様上 col=0（左側）のみ。col=1（右側＝白/スキル列）で
        # detect_factor_color が緑誤判定するケース（受領 1558 parent1/parent2、
        # 1814 main/parent1 等）があり、そこを採用するとレース名行の OCR 結果を
        # 緑 name にしてしまう事故が発生する。col=1 は名前採用候補から除外。
        # ただし★数は col=1 でも偽陽性タイルとして★が正しく取れていることが
        # 多いため、any_green_gold で保持して後段 Pass 2 の★補填に使う。
        if box.col_index != 0:
            continue
        dc = _display_crop_from_original(img_orig, box.bbox, scale)
        raw, frags = ocr.recognize_with_parts(dc)
        cands = ocr.match_to_green_factor_multi(raw, frags, top_k=1)
        top_conf = cands[0][1] if cands else 0.0
        # OCR が空 / 低スコアでも、テンプレマッチで box を比較できるよう
        # 緑名前テンプレ top1 のスコアも加味する。同 uma 内に row=1 col=0 と
        # row=2 col=0 の両方が color=green と判定されるケース（umamusume_182056 等）
        # で、テンプレマッチが高スコアの方を best_green_box に選べる。
        _gnx0, _gny0, _gnx1, _gny1 = box.bbox
        _gn_x1 = _gnx0 + int((_gnx1 - _gnx0) * 0.85)
        _gn_crop = _display_crop_from_original(
            img_orig, (_gnx0, _gny0, _gn_x1, _gny1), scale, pad_y_norm=2,
        )
        _gn_matches = match_green_name(_gn_crop)
        _gn_conf = _gn_matches[0][1] if _gn_matches else 0.0
        combined_conf = max(top_conf, _gn_conf)
        uidx = box.uma_index
        if combined_conf > best_green_score.get(uidx, 0.0):
            best_green_score[uidx] = combined_conf
            best_green_box[uidx] = box
        g = box.gold_star_count or 0
        if g > best_green_gold.get(uidx, 0):
            best_green_gold[uidx] = g

    for box in boxes:
        rank_crop_orig = _crop_rank_from_original(img_orig, box.bbox, scale, box.rank_bbox)
        x0, y0, x1, y1 = box.bbox
        text_crop_norm = norm_img[y0:y1, x0:x1]

        # 色チップ検出が弱い合成画像で box.color が "white" / 逆の青赤 に落ちても、
        # 因子が常に決まった位置に並ぶゲーム UI 構造を使って位置で補正する。
        #   row 0 col 0 → 青因子（左上）
        #   row 0 col 1 → 赤因子（青の右）
        #   row 1 col 0 → 緑因子（青の下）
        # は必ず存在するため、この 3 セルは位置で絶対確定し color 判定は無視する。
        # これで「緑因子が色判定 white に落ちて白スキル行に混入」「row 0 col 1 が
        # 青と誤判定され blue slot に先取りされる」等の事故を防ぐ。
        # row>=2 col=0 で色判定 green になる box（稀だが本物緑が cropper の
        # 都合で row=2 側に検出される画像あり）は fallback で緑スロット候補に残す。
        if box.row_index == 0 and box.col_index == 0:
            is_blue_slot, is_red_slot, is_green_slot = True, False, False
        elif box.row_index == 0 and box.col_index == 1:
            is_blue_slot, is_red_slot, is_green_slot = False, True, False
        elif box.row_index == 1 and box.col_index == 0:
            is_blue_slot, is_red_slot, is_green_slot = False, False, True
        else:
            is_blue_slot = box.color == "blue"
            is_red_slot = box.color == "red"
            # col=1 の色判定 green（レース名スキルの緑アイコン等）は除外。
            is_green_slot = box.color == "green" and box.col_index == 0

        # この box が本当に緑スロットとして採用される見込みがあるか。
        # is_green_slot=True でも best_green_box に選ばれなかった box は結局
        # skills 行きになるため、緑辞書（249 種の固有スキル）OCR 処理をすると
        # skills に緑辞書マッチ名が紛れ込む（例: '白い稲妻、見せたるで！'）。
        # 緑として採用される見込みがない box は通常 OCR ルートに流し、
        # 緑辞書は緑スロットに限定する。uma.green_name の逐次状態で先着判定。
        uidx_cur = box.uma_index
        uma_cur_green_name = umas[uidx_cur].green_name
        best_box_cur = best_green_box.get(uidx_cur)
        best_conf_cur = best_green_score.get(uidx_cur, 0.0)
        if is_green_slot and not uma_cur_green_name:
            if best_box_cur is not None and best_conf_cur >= 0.5:
                green_adoptable = box is best_box_cur
            else:
                # OCR 確信度が低い場合の fallback。
                # 位置絶対 row=1 col=0 box は緑タイル内★が HSV で拾えない
                # 画像（umamusume_* 等）でも必ず緑因子が存在するため、
                # 同 uma 内に他の色判定 green box がない場合に限り強制採用。
                # 他に色判定 green box がある場合は、そちらが OCR で正解を
                # 出している可能性が高いため従来の ★>0 条件で判定する。
                same_uma_green_others = any(
                    b for b in boxes
                    if b.uma_index == uidx_cur
                    and b.color == "green"
                    and b.col_index == 0
                    and not (b.row_index == 1 and b.col_index == 0)
                )
                pos_absolute = (
                    box.row_index == 1
                    and box.col_index == 0
                    and not same_uma_green_others
                )
                if pos_absolute:
                    green_adoptable = True
                else:
                    green_adoptable = (
                        box.gold_star_count is None or box.gold_star_count > 0
                    )
        else:
            green_adoptable = False

        # 青スロットは box.bbox が★中心基準で算出されており、一部画像で因子名
        # テキストが bbox 下端からはみ出して OCR 入力に映らない問題がある。
        # display_crop の pad_y_norm を 8 に拡大することで「スピード」「スタミナ」等が
        # OCR で拾えるようになり青 +3 件改善を確認。
        # 赤は pad_y_norm 両方向拡張（Exp 3）で「長距離→マイル」悪化が発生したが、
        # 真因は bbox が★中心基準で上にズレてテキストが下端にはみ出すこと。
        # display_crop の元 bbox を y1 のみ +14 に拡張（y0 は維持）で、上の行を
        # 含まずテキストだけを入れる非対称 pad に変更する。
        if is_blue_slot:
            display_crop = _display_crop_from_original(
                img_orig, box.bbox, scale, pad_y_norm=8
            )
        elif is_red_slot:
            img_h = norm_img.shape[0]
            red_disp_bbox = (x0, y0, x1, min(img_h, y1 + 14))
            display_crop = _display_crop_from_original(
                img_orig, red_disp_bbox, scale, pad_y_norm=2
            )
        else:
            display_crop = _display_crop_from_original(img_orig, box.bbox, scale)
        ext_bbox = box.bbox
        ext_text_crop_norm = text_crop_norm

        # ONNX 候補
        if is_blue_slot:
            crops = [
                _crop_from_original(img_orig, ext_bbox, scale, dy, dx)
                for dy, dx in PERTURBATIONS_BLUE
            ]
            crops.append(ext_text_crop_norm)
            onnx_candidates = factor_pred.topk_in_category(crops, BLUE_FACTOR_TYPES, k=5)
        elif is_red_slot:
            crops = [
                _crop_from_original(img_orig, ext_bbox, scale, dy, dx)
                for dy, dx in PERTURBATIONS_RED
            ]
            crops.append(ext_text_crop_norm)
            onnx_candidates = factor_pred.topk_in_category(
                crops, RED_FACTOR_TYPES, k=5, use_multi_interp=True
            )
        else:
            text_crop_orig = _crop_from_original(img_orig, box.bbox, scale)
            onnx_candidates = factor_pred.topk_ensemble(
                [text_crop_orig, text_crop_norm], k=5
            )

        # OCR 候補（display_crop を使う。テキスト全域が入っているため）
        # 赤/青スロットは allowlist 付き OCR でゴミ文字を抑制
        # （'2', ']' 等の雑音を除外し、候補を BLUE/RED_FACTOR_TYPES の構成文字に限定）
        # 緑は断片分割 OCR で「連結+断片」並列マッチして長文アンカー寄せを抑制。
        # row 0 位置絶対化で「色=緑だが青/赤スロット」のケースが出るため、
        # 分岐は is_*_slot を最優先し、緑判定はその後で評価する。
        ocr_fragments: list[str] = []
        if is_red_slot:
            ocr_raw = ocr.recognize_red(display_crop)
        elif is_blue_slot:
            ocr_raw = ocr.recognize_blue(display_crop)
        elif green_adoptable:
            ocr_raw, ocr_fragments = ocr.recognize_with_parts(display_crop)
        else:
            ocr_raw = ocr.recognize(display_crop)
        if green_adoptable:
            # 緑は固有スキル辞書 249 件 + 断片並列マッチで誤マッチを抑制。
            # 緑として採用される見込みがある box に限定することで、skills に
            # 緑辞書マッチ名が混入する副作用を防ぐ。
            ocr_candidates = ocr.match_to_green_factor_multi(
                ocr_raw, ocr_fragments, top_k=5
            )
        else:
            ocr_candidates = ocr.match_to_factor(ocr_raw, top_k=5)
        # 青/赤はカテゴリ外の候補を除外（位置ベース判定も含む）
        if is_blue_slot:
            ocr_candidates = [(n, s) for n, s in ocr_candidates if n in BLUE_FACTOR_TYPES]
        elif is_red_slot:
            ocr_candidates = [(n, s) for n, s in ocr_candidates if n in RED_FACTOR_TYPES]
        elif not green_adoptable:
            # 白スキル/青赤誤流入などの非緑スロットでは、ONNX の top-k に緑因子
            # （固有スキル 249 件）が混ざると skills に '恵福バルカローレ' のような
            # 緑専用名が紛れ込む。緑辞書は緑スロットにのみ適用するため ONNX 側も
            # 除外する。match_to_factor 側は辞書ロード時点で緑除外済み。
            onnx_candidates = [(n, s) for n, s in onnx_candidates if n not in green_name_set]

        # テンプレートマッチ候補。
        # datasets/{red_blue_templates, green_name_templates}/ の正解 crop と
        # display_crop を比較し、ピアソン相関最大のカテゴリを採用する。
        # 低解像度で OCR/ONNX が失敗する画像でも「既知の正解形」に最も似ている
        # カテゴリを選べる強力なシグナル。
        template_candidates: list[tuple[str, float]] = []
        if is_red_slot:
            template_candidates = match_templates(display_crop, "red")[:5]
        elif is_blue_slot:
            template_candidates = match_templates(display_crop, "blue")[:5]
        elif green_adoptable:
            # 緑因子タイルの名前領域（左 85%）をテンプレと比較
            _nx0, _ny0, _nx1, _ny1 = box.bbox
            _name_x1 = _nx0 + int((_nx1 - _nx0) * 0.85)
            _name_crop = _display_crop_from_original(
                img_orig, (_nx0, _ny0, _name_x1, _ny1), scale, pad_y_norm=2
            )
            template_candidates = match_green_name(_name_crop)[:5]

        # マージ（緑スロットは OCR top1 が正解を出すケースでも全 813 辞書の ONNX top1 に
        # 押し負けやすいため、ocr_strong_threshold を 0.5 に緩和して OCR を優先する）
        merge_threshold = 0.5 if green_adoptable else 0.7
        merged, sources = _merge_candidates(
            onnx_candidates,
            ocr_candidates,
            limit=8,
            ocr_strong_threshold=merge_threshold,
        )

        # 赤/青/緑スロットでテンプレマッチ top1 が強い場合、最終 top_name として採用する。
        # merged 側に top_name が存在しない場合もあるため、候補として追加する。
        # 閾値: 赤/青(10/5 カテゴリ)は 0.90、緑名前(46 カテゴリ)はサンプル数が
        # 偏るため 0.95 と厳しめに。
        if template_candidates:
            t_name, t_score = template_candidates[0]
            t_threshold = 0.95 if green_adoptable else 0.90
            if t_score >= t_threshold:
                merged = [(t_name, t_score)] + [(n, s) for n, s in merged if n != t_name]
                sources[t_name] = "template"
        top_name = merged[0][0] if merged else ""

        # ★数は金★の実数カウントを最優先（rank モデルより高精度な実測値）。
        # ただし金★検出の HSV 閾値次第で暗めの金★を取りこぼすケースがあり、
        # gold_star_count==0 だと実際は★1以上あるのに★0と誤認する可能性がある。
        # そのため gold_star_count が 0 の場合は rank モデル推論にフォールバックする。
        if box.gold_star_count is not None and box.gold_star_count > 0:
            star = box.gold_star_count
        else:
            rpred = rank_pred.predict_with_perturbation(rank_crop_orig, PERTURBATIONS_RANK)
            try:
                star = int(rpred.label)
            except ValueError:
                star = 0
            # row 0 の青/赤スロット（col 0/1 とも因子が必ず存在する UI 構造）で
            # rank モデルが低信頼度で★0 を返す場合は、HSV 検出漏れとみなし
            # 最低★1 を保証する。★2+ を★1 として過少記録するリスクはあるが、
            # ★0 誤認（因子未記録）よりは許容できる。
            if (
                star == 0
                and box.row_index == 0
                and box.col_index in (0, 1)
                and rpred.confidence < 0.6
            ):
                star = 1

        uma = umas[box.uma_index]
        slot_kind: str
        white_idx = 0
        # 緑採用は OCR 分岐前に判定した green_adoptable と同一。
        # （uma.green_name 空の条件は green_adoptable 内に含む）
        uidx = box.uma_index
        green_ok = green_adoptable
        if is_blue_slot and top_name in BLUE_FACTOR_TYPES and not uma.blue_type:
            uma.blue_type = top_name
            # ★数はテンプレマッチで高確信の場合に上書き
            _bx0, _by0, _bx1, _by1 = box.bbox
            _b_right_x0 = _bx0 + int((_bx1 - _bx0) * 0.5)
            _b_star_crop = _display_crop_from_original(
                img_orig, (_b_right_x0, _by0, _bx1, _by1), scale, pad_y_norm=2
            )
            _b_star_matches = match_star(_b_star_crop, "blue")
            if _b_star_matches and _b_star_matches[0][1] >= 0.92:
                uma.blue_star = _b_star_matches[0][0]
            else:
                uma.blue_star = star
            slot_kind = "blue"
        elif is_red_slot and top_name in RED_FACTOR_TYPES and not uma.red_type:
            uma.red_type = top_name
            _rx0, _ry0, _rx1, _ry1 = box.bbox
            _r_right_x0 = _rx0 + int((_rx1 - _rx0) * 0.5)
            _r_star_crop = _display_crop_from_original(
                img_orig, (_r_right_x0, _ry0, _rx1, _ry1), scale, pad_y_norm=2
            )
            _r_star_matches = match_star(_r_star_crop, "red")
            if _r_star_matches and _r_star_matches[0][1] >= 0.92:
                uma.red_star = _r_star_matches[0][0]
            else:
                uma.red_star = star
            slot_kind = "red"
        elif green_ok:
            uma.green_name = top_name
            # 緑の★数決定の優先順位（上から採用）:
            #  1. 緑★テンプレートマッチ（datasets/star_templates/green/）で
            #     高確信（score >= 0.92）の結果があればそれを最優先。HSV 実測で
            #     拾えない umamusume 系画像で唯一精度が出せる方式。
            #  2. 自身の gold_star_count（HSV+CNN で実測）
            #  3. 同 uma の緑色判定 box の最も近い gold（テキスト行/★行分裂救済）
            #  4. 既に計算済みの rank モデル推論結果 star（HSV 失敗 fallback）
            #  5. 緑因子は固有スキル＝必ず★>=1 なので最低★1 を保証
            # テンプレは green_tile 右半分（★領域）を 64×16 にリサイズしたもの。
            _gx0, _gy0, _gx1, _gy1 = box.bbox
            _g_right_x0 = _gx0 + int((_gx1 - _gx0) * 0.5)
            _g_star_crop = _display_crop_from_original(
                img_orig, (_g_right_x0, _gy0, _gx1, _gy1), scale, pad_y_norm=2
            )
            _star_matches = match_green_star(_g_star_crop)
            if _star_matches and _star_matches[0][1] >= 0.92:
                uma.green_star = _star_matches[0][0]
            else:
                own_gold = box.gold_star_count or 0
                if own_gold > 0:
                    uma.green_star = own_gold
                else:
                    nearest_star = 0
                    best_dist = None
                    for b in boxes:
                        if b.uma_index != uidx or b.color != "green":
                            continue
                        g = b.gold_star_count or 0
                        if g <= 0:
                            continue
                        d = abs(b.row_index - box.row_index)
                        if best_dist is None or d < best_dist:
                            best_dist = d
                            nearest_star = g
                    if nearest_star > 0:
                        uma.green_star = nearest_star
                    elif star > 0:
                        # rank モデル推論の結果（HSV 検出が弱い画像の fallback）
                        uma.green_star = star
                    else:
                        # 緑因子は固有スキル、必ず★1 以上存在する。最低保証。
                        uma.green_star = 1
            slot_kind = "green"
        else:
            uma.skills.append(FactorEntry(color=box.color, name=top_name, star=star))
            white_idx = white_counters[box.uma_index]
            white_counters[box.uma_index] += 1
            slot_kind = "white"

        review.add(
            ReviewItem(
                uma_index=box.uma_index,
                uma_role=UMA_ROLES[box.uma_index],
                slot=slot_kind,  # type: ignore[arg-type]
                white_index=white_idx,
                image=display_crop.copy(),
                candidates=merged,
                candidate_sources=sources,
                ocr_raw=ocr_raw,
                current_name=top_name,
                current_star=star,
            )
        )

    # Pass 2: 緑 col=1 除外により uma.green_name が未採用だが、★は col=1 に
    # 残っているケースがある（受領 1558 parent1/parent2 等）。name は空のまま
    # ★だけ any_green_gold から補填する。評価時に name 誤認件数は変わらないが、
    # ★は正解にできる＝★悪化を防げる。
    for uma_idx, uma in enumerate(umas):
        if not uma.green_name and uma.green_star == 0:
            g = any_green_gold.get(uma_idx, 0)
            if g > 0:
                uma.green_star = g

    # 緑因子（固有スキル）から character を逆引き：
    # character は ONNX の画像分類だと衣装差などで誤判定しやすいが、
    # 固有スキルは一意に衣装（カード）を決めるため、マッピングが一致する場合は
    # そちらを優先する。マッピングに無い場合は ONNX 結果を残す。
    # 注: 継承タブ画像（親由来の継承スキル）では逆引き先が自分の衣装と一致せず
    # 誤上書きする副作用があるが、育成情報タブ画像での精度向上を優先するため
    # 無条件適用とする。タブ種別が画像から判別できるようになれば再検討する。
    unique_map = load_unique_skill_to_character()
    if unique_map:
        for uma in umas:
            if uma.green_name and uma.green_name in unique_map:
                uma.character = unique_map[uma.green_name]

    import os
    submission = Submission(
        submitter_id=submitter_id,
        image_filename=os.path.basename(image_path),
        main=umas[0],
        parent1=umas[1],
        parent2=umas[2],
    )
    return submission, review


def apply_review_results(submission: Submission, review: ReviewQueue) -> None:
    """ユーザレビュー後の ReviewItem の reviewed_name / reviewed_star を Submission に反映。"""
    umas = [submission.main, submission.parent1, submission.parent2]
    for item in review.items:
        if item.reviewed_name is None:
            continue
        uma = umas[item.uma_index]
        star = item.reviewed_star if item.reviewed_star is not None else item.current_star
        if item.slot == "blue":
            uma.blue_type = item.reviewed_name
            uma.blue_star = star
        elif item.slot == "red":
            uma.red_type = item.reviewed_name
            uma.red_star = star
        elif item.slot == "green":
            uma.green_name = item.reviewed_name
            uma.green_star = star
        elif item.slot == "white":
            if 0 <= item.white_index < len(uma.skills):
                uma.skills[item.white_index].name = item.reviewed_name
                uma.skills[item.white_index].star = star


def _dump_debug_crops(
    img: np.ndarray,
    sections: list[CharaSection],
    boxes: list[FactorBox],
    out_dir: str,
) -> None:
    import os

    os.makedirs(out_dir, exist_ok=True)
    overlay = img.copy()
    for s in sections:
        x0, y0, x1, y1 = s.portrait_bbox
        cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 255, 0), 2)
    for b in boxes:
        x0, y0, x1, y1 = b.bbox
        color = {"blue": (255, 0, 0), "red": (0, 0, 255), "green": (0, 255, 0)}.get(
            b.color, (255, 255, 255)
        )
        cv2.rectangle(overlay, (x0, y0), (x1, y1), color, 1)
    cv2.imwrite(os.path.join(out_dir, "_overlay.png"), overlay)
    for b in boxes:
        base = f"uma{b.uma_index}_row{b.row_index:02d}_col{b.col_index}_{b.color}"
        cv2.imwrite(os.path.join(out_dir, f"{base}_text.png"), b.text_img)
        cv2.imwrite(os.path.join(out_dir, f"{base}_rank.png"), b.rank_img)

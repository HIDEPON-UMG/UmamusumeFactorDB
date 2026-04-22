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
) -> np.ndarray:
    """レビュー UI 表示専用の広めクロップ。

    モデル入力領域（recognizer.json の left_rect / right_rect）は因子名テキストの
    左端が欠ける場合があるため、UI で確認しやすいよう左右と上下にパディングを追加する。
    モデル推論には使わない（精度影響なし）。
    """
    inv = 1.0 / scale if scale != 0 else 1.0
    x0, y0, x1, y1 = bbox
    # 正規化 540 基準で 左 +32px、右 +8px、上下 +2px の余白
    PAD_LEFT_NORM = 32
    PAD_RIGHT_NORM = 8
    PAD_Y_NORM = 2
    ox0 = int(round((x0 - PAD_LEFT_NORM) * inv))
    oy0 = int(round((y0 - PAD_Y_NORM) * inv))
    ox1 = int(round((x1 + PAD_RIGHT_NORM) * inv))
    oy1 = int(round((y1 + PAD_Y_NORM) * inv))
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

    umas = [UmaFactors(), UmaFactors(), UmaFactors()]
    review = ReviewQueue()
    white_counters = {0: 0, 1: 0, 2: 0}

    for section in sections:
        icon = _extract_character_icon_bgr(norm_img, section)
        pred = char_pred.predict(icon)
        umas[section.uma_index].character = pred.label

    for box in boxes:
        rank_crop_orig = _crop_rank_from_original(img_orig, box.bbox, scale, box.rank_bbox)
        x0, y0, x1, y1 = box.bbox
        text_crop_norm = norm_img[y0:y1, x0:x1]
        display_crop = _display_crop_from_original(img_orig, box.bbox, scale)

        # 色チップ検出が弱い合成画像で box.color が "white" / 逆の青赤 に落ちても、
        # 青/赤因子が常に row 0 の左/右列に並ぶゲーム UI 構造を使って位置で補正する。
        # row 0 は位置で絶対確定（col=0 → 青、col=1 → 赤）し、color 判定は無視する。
        # これで「row 0 col 1 が青と誤判定され blue slot に先取りされる」事故を防ぐ。
        if box.row_index == 0 and box.col_index == 0:
            is_blue_slot = True
            is_red_slot = False
        elif box.row_index == 0 and box.col_index == 1:
            is_blue_slot = False
            is_red_slot = True
        else:
            is_blue_slot = box.color == "blue"
            is_red_slot = box.color == "red"

        # ONNX 候補
        if is_blue_slot:
            crops = [
                _crop_from_original(img_orig, box.bbox, scale, dy, dx)
                for dy, dx in PERTURBATIONS_BLUE
            ]
            crops.append(text_crop_norm)
            onnx_candidates = factor_pred.topk_in_category(crops, BLUE_FACTOR_TYPES, k=5)
        elif is_red_slot:
            crops = [
                _crop_from_original(img_orig, box.bbox, scale, dy, dx)
                for dy, dx in PERTURBATIONS_RED
            ]
            crops.append(text_crop_norm)
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
        elif box.color == "green":
            ocr_raw, ocr_fragments = ocr.recognize_with_parts(display_crop)
        else:
            ocr_raw = ocr.recognize(display_crop)
        if not is_red_slot and not is_blue_slot and box.color == "green":
            # 緑は固有スキル辞書 249 件 + 断片並列マッチで誤マッチを抑制
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

        # マージ（緑スロットは OCR top1 が正解を出すケースでも全 813 辞書の ONNX top1 に
        # 押し負けやすいため、ocr_strong_threshold を 0.5 に緩和して OCR を優先する）
        merge_threshold = 0.5 if (not is_red_slot and not is_blue_slot and box.color == "green") else 0.7
        merged, sources = _merge_candidates(
            onnx_candidates,
            ocr_candidates,
            limit=8,
            ocr_strong_threshold=merge_threshold,
        )
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
        # is_blue_slot / is_red_slot は ONNX 推論ブロックで位置ベース補正込みに算出済み。
        # 緑は重複検出（左端アイコン偽陽性）が出やすいので gold_star_count>0 の
        # 候補を優先し、gold_star_count==0 の緑 box は skill へ回す（後続の真緑 box
        # に green スロットの採用機会を残すため）。
        green_ok = box.color == "green" and not uma.green_name and (
            box.gold_star_count is None or box.gold_star_count > 0
        )
        if is_blue_slot and top_name in BLUE_FACTOR_TYPES and not uma.blue_type:
            uma.blue_type = top_name
            uma.blue_star = star
            slot_kind = "blue"
        elif is_red_slot and top_name in RED_FACTOR_TYPES and not uma.red_type:
            uma.red_type = top_name
            uma.red_star = star
            slot_kind = "red"
        elif green_ok:
            uma.green_name = top_name
            uma.green_star = star
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

    # 緑因子（固有スキル）から character を逆引き：
    # character は ONNX の画像分類だと衣装差などで誤判定しやすいが、
    # 固有スキルは一意に衣装（カード）を決めるため、マッピングが一致する場合は
    # そちらを優先する。マッピングに無い場合は ONNX 結果を残す。
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

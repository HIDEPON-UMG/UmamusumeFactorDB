"""★誤認 9 件の内部状態を可視化して根本原因を診断する。

各誤認行について、以下を出力する:
  - HSV で検出された金★候補数 / 空★候補数（bbox サイズと位置）
  - CNN で分類した結果（各候補のラベル・confidence）
  - 最終 gold_count（★数）と正解★数のズレ
  - bbox 内の★候補位置のオーバーレイ画像を debug_star_errors/ に保存

使い方:
    python scripts/diagnose_star_errors.py
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umafactor.cropper import (  # noqa: E402
    BASE_WIDTH,
    _detect_golden_stars,
    _detect_empty_stars,
    _cluster_stars_into_rows,
    _estimate_tile_right_edges,
    _assign_row_to_section,
    detect_chara_sections,
    normalize_width,
    TILE_WIDTH,
    STAR_Y_IN_TILE,
    TILE_HEIGHT,
)
from umafactor.infer import predict_stars_batch  # noqa: E402


# 誤認行（evaluate_labels 出力より）
ERROR_CASES = [
    ("receipt_20260421031432408.png", "main", "red", 2, 1),  # 認識★, 正解★
    ("receipt_20260421031558457.png", "main", "green", 3, 2),
    ("receipt_20260421031814474.png", "main", "red", 0, 1),
    ("receipt_20260421031814474.png", "main", "green", 2, 1),
    ("receipt_20260421031814474.png", "parent1", "green", 1, 2),
    ("receipt_20260421031832634.png", "main", "red", 0, 1),
    ("receipt_20260421031851324.png", "parent1", "blue", 1, 3),
    ("receipt_20260421032331541.png", "parent2", "red", 0, 3),
    ("sample_oguricap.png", "main", "green", 0, 2),
]


def imread_unicode(path: Path):
    buf = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def imwrite_unicode(path: Path, img):
    ok, buf = cv2.imencode(path.suffix, img)
    if ok:
        buf.tofile(str(path))


def role_to_uma_index(role: str) -> int:
    return {"main": 0, "parent1": 1, "parent2": 2}[role]


def crop_slot_for_cnn(img: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    cx, cy = x + w // 2, y + h // 2
    half = max(w, h) // 2 + 4
    return img[
        max(0, cy - half) : min(img.shape[0], cy + half),
        max(0, cx - half) : min(img.shape[1], cx + half),
    ]


def diagnose_image(img_path: Path, cases: list[tuple]) -> list[dict]:
    img_orig = imread_unicode(img_path)
    if img_orig is None:
        return []
    img, _ = normalize_width(img_orig, BASE_WIDTH)
    gold = _detect_golden_stars(img)
    empty = _detect_empty_stars(img)
    classified = _cluster_stars_into_rows(gold, empty, img.shape[1])

    try:
        sections = detect_chara_sections(img)
    except RuntimeError:
        sections = []
    x_L1, x_R1 = _estimate_tile_right_edges(classified) if classified else (None, None)
    x_L0 = max(0, (x_L1 or 0) - TILE_WIDTH)
    x_R0 = max(0, (x_R1 or 0) - TILE_WIDTH)

    # row_index をセクション内で 0 から数える（cropper と同じロジック）
    per_section_row_idx: dict[int, int] = {}
    row_entries = []
    for y_center, lg, rg, le, re_ in classified:
        uma_idx = _assign_row_to_section(y_center, sections) if sections else None
        if uma_idx is None:
            continue
        row_idx = per_section_row_idx.get(uma_idx, 0)
        per_section_row_idx[uma_idx] = row_idx + 1
        row_entries.append((uma_idx, row_idx, y_center, lg, rg, le, re_))

    results = []
    for case_img, role, color, got_star, expected_star in cases:
        target_uma = role_to_uma_index(role)
        # color から col を推定（blue/green=left, red=right だが、位置は様々）
        # シンプルに color == 'red' → col=1, それ以外 → col=0 と仮定
        # 実際は pipeline 側で位置ベース補正があるので、ここでは「該当 uma の全行」を見る
        per_uma_rows = [r for r in row_entries if r[0] == target_uma]

        case_detail = {
            "image": case_img,
            "role": role,
            "color": color,
            "got": got_star,
            "expected": expected_star,
            "rows": [],
        }
        for uma_idx, row_idx, y_center, lg, rg, le, re_ in per_uma_rows:
            for col_idx, (col_gold, col_empty) in enumerate([(lg, le), (rg, re_)]):
                xa = x_L0 if col_idx == 0 else x_R0
                xb = x_L1 if col_idx == 0 else x_R1
                if xa is None or xb is None:
                    continue
                # 右 60% フィルタ（緑因子の左端●除外） — 色が分からないので両方見る
                candidates = col_gold + col_empty
                if not candidates:
                    continue
                # CNN 推論
                slot_imgs = []
                for (sx, sy, sw, sh) in candidates:
                    slot = crop_slot_for_cnn(img, sx, sy, sw, sh)
                    if slot.size > 0:
                        slot_imgs.append(slot)
                    else:
                        slot_imgs.append(np.zeros((28, 28, 3), dtype=np.uint8))
                classifications = predict_stars_batch(slot_imgs)
                case_detail["rows"].append({
                    "row_idx": row_idx,
                    "col_idx": col_idx,
                    "y_center": y_center,
                    "bbox_x": [int(xa), int(xb)],
                    "hsv_gold_n": len(col_gold),
                    "hsv_empty_n": len(col_empty),
                    "candidates": [
                        {
                            "source": "gold" if i < len(col_gold) else "empty",
                            "xywh": [int(c[0]), int(c[1]), int(c[2]), int(c[3])],
                            "cnn_label": classifications[i][0],
                            "cnn_conf": round(classifications[i][1], 3),
                        }
                        for i, c in enumerate(candidates)
                    ],
                })
        results.append(case_detail)

    # デバッグ画像: 該当行の bbox と ★候補をオーバーレイ
    dbg_dir = PROJECT_ROOT / "debug_star_errors"
    dbg_dir.mkdir(exist_ok=True)
    for case in results:
        if not case["rows"]:
            continue
        vis = img.copy()
        for r in case["rows"]:
            # bbox 矩形
            xa, xb = r["bbox_x"]
            y_top = max(0, r["y_center"] - STAR_Y_IN_TILE)
            y_bot = y_top + TILE_HEIGHT
            cv2.rectangle(vis, (xa, y_top), (xb, y_bot), (0, 255, 255), 1)
            cv2.putText(
                vis, f"r{r['row_idx']}c{r['col_idx']}",
                (xa, y_top - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1,
            )
            for c in r["candidates"]:
                x, y, w, h = c["xywh"]
                color = (0, 255, 0) if c["cnn_label"] == "gold" else (255, 128, 0)
                cv2.rectangle(vis, (x, y), (x + w, y + h), color, 1)
                cv2.putText(
                    vis, f"{c['cnn_label'][0]}{c['cnn_conf']:.2f}",
                    (x, y + h + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, color, 1,
                )
        out = dbg_dir / f"{case['image']}__{case['role']}_{case['color']}__got{case['got']}_exp{case['expected']}.png"
        imwrite_unicode(out, vis)

    return results


def main() -> None:
    fixtures = PROJECT_ROOT / "tests" / "fixtures"
    dbg_dir = PROJECT_ROOT / "debug_star_errors"
    if dbg_dir.exists():
        shutil.rmtree(dbg_dir)

    # 画像ごとにまとめる
    by_image: dict[str, list[tuple]] = {}
    for case in ERROR_CASES:
        by_image.setdefault(case[0], []).append(case)

    all_results = []
    for img_name, cases in by_image.items():
        path = fixtures / img_name
        if not path.exists():
            print(f"skip: {path}")
            continue
        print(f"=== {img_name} ===")
        results = diagnose_image(path, cases)
        all_results.extend(results)
        for r in results:
            print(f"  [{r['role']}/{r['color']}] got={r['got']} expected={r['expected']}")
            for row in r["rows"]:
                cand_summary = ", ".join(
                    f"{c['source']}:{c['cnn_label']}({c['cnn_conf']})"
                    for c in row["candidates"]
                )
                gold_via_cnn = sum(1 for c in row["candidates"] if c["cnn_label"] == "gold")
                print(
                    f"    r{row['row_idx']}c{row['col_idx']} "
                    f"y={row['y_center']} "
                    f"hsv(g={row['hsv_gold_n']},e={row['hsv_empty_n']}) "
                    f"cnn_gold={gold_via_cnn}  [{cand_summary}]"
                )
        print()

    summary_json = PROJECT_ROOT / "debug_star_errors" / "summary.json"
    summary_json.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"詳細: {summary_json}")


if __name__ == "__main__":
    main()

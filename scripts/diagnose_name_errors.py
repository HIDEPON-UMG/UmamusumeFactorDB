"""因子名誤認の内訳を診断する。

evaluate_labels.py と同じロジックで details を生成し、
name_hit == False の行を色・パターン別に集計する。
特に赤因子の距離系（短距離/マイル/中距離/長距離）の混同を重点分析。

使い方:
    python scripts/diagnose_name_errors.py
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LABELS_CSV = PROJECT_ROOT / "tests" / "fixtures" / "labels_2026-04-20T18-54-21.csv"
RESULTS_JSON = PROJECT_ROOT / "tests" / "fixtures" / "colored_factors" / "recognition_results.json"

DISTANCE_SET = {"短距離", "マイル", "中距離", "長距離"}


def get_slot(rec: dict, image: str, role: str, color: str) -> str:
    img_rec = rec.get(image, {})
    if "error" in img_rec:
        return ""
    role_data = img_rec.get(role, {}) or {}
    field = role_data.get(color, {}) or {}
    return field.get("type") or field.get("name") or ""


def main() -> None:
    with LABELS_CSV.open(encoding="utf-8-sig") as f:
        labels = list(csv.DictReader(f))
    rec = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))

    rows = [r for r in labels if r["status"] == "edited"]
    total = len(rows)

    wrong_by_color: dict[str, list] = defaultdict(list)
    distance_confusion: Counter = Counter()
    for r in rows:
        image = r["image_name"]
        role = r["role"]
        color = r["color"]
        correct = r["correct_value"]
        rec_value = get_slot(rec, image, role, color)
        if rec_value != correct:
            wrong_by_color[color].append({
                "image": image,
                "role": role,
                "correct": correct,
                "recognized": rec_value or "(empty)",
            })
            if color == "red" and correct in DISTANCE_SET:
                distance_confusion[(correct, rec_value)] += 1

    name_wrong_total = sum(len(v) for v in wrong_by_color.values())
    print(f"=== 因子名誤認の内訳 ===")
    print(f"対象 edited 行: {total}, 因子名誤認: {name_wrong_total}")
    print()

    for color in ("blue", "red", "green", "white"):
        lst = wrong_by_color.get(color, [])
        if not lst:
            continue
        print(f"--- {color}: {len(lst)} 件 ---")
        for item in lst:
            print(
                f"  {item['image']} / {item['role']}: "
                f"'{item['correct']}' → '{item['recognized']}'"
            )
        print()

    print("--- 赤因子の距離系混同ペア ---")
    if not distance_confusion:
        print("  距離系の混同は検出されませんでした。")
    else:
        for (correct, rec_value), n in distance_confusion.most_common():
            print(f"  {correct} → {rec_value or '(empty)'}: {n} 件")


if __name__ == "__main__":
    main()
